"""LLM gateway proxy with streaming PII redaction.

POST /v1/chat/completions (OpenAI-compatible). Requests are forwarded to the
upstream provider; responses, streamed or not, have emails, SSNs and card
numbers replaced with [REDACTED] before they reach the client.

Run with:
    uvicorn task3_pii_stream_guardrail.app:create_app_from_env --factory --port 8100
"""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from task3_pii_stream_guardrail.redactor import redact_text
from task3_pii_stream_guardrail.sse import redact_sse, stream_error_event

log = logging.getLogger("task3.app")


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    upstream_api_key: str | None = None
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 60.0  # longest silence allowed between upstream chunks
    max_body_bytes: int = 1_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_url=os.environ.get("UPSTREAM_URL", "http://127.0.0.1:9201/v1/chat/completions"),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY"),
        )


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if client is not None:
            yield
            return
        timeout = httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as owned:
            app.state.client = owned
            yield

    app = FastAPI(title="LLM gateway PII guardrail", lifespan=lifespan)
    app.state.client = client

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            return _error(413, "request_too_large", "Request body too large")
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "Request body must be JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return _error(400, "invalid_request", "Request must be a JSON object with a messages list")

        headers = {"Content-Type": "application/json"}
        if settings.upstream_api_key:
            headers["Authorization"] = f"Bearer {settings.upstream_api_key}"
        streaming = body.get("stream") is True
        upstream_request = app.state.client.build_request("POST", settings.upstream_url, json=body, headers=headers)

        try:
            upstream = await app.state.client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            log.error("upstream request failed: %s", type(exc).__name__)
            return _error(502, "upstream_unavailable", "The model provider is unavailable")

        if upstream.status_code != 200:
            await upstream.aclose()
            log.warning("upstream returned HTTP %d", upstream.status_code)
            status = 429 if upstream.status_code == 429 else 502
            return _error(status, "upstream_error", "The model provider could not process the request")

        if not streaming:
            try:
                await upstream.aread()
                result = upstream.json()
            except (httpx.HTTPError, ValueError):
                return _error(502, "upstream_error", "The model provider returned an invalid response")
            finally:
                await upstream.aclose()
            for choice in result.get("choices", []) if isinstance(result, dict) else []:
                message = choice.get("message") if isinstance(choice, dict) else None
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    message["content"] = redact_text(message["content"])
            return JSONResponse(result)

        async def events():
            try:
                async for event in redact_sse(upstream.aiter_lines()):
                    yield event
            except httpx.HTTPError as exc:
                # Headers are already sent, so the failure is reported in-stream.
                log.error("upstream stream broke: %s", type(exc).__name__)
                yield stream_error_event("upstream_error", "The model provider stream was interrupted")

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            # X-Accel-Buffering stops nginx-style proxies from buffering the
            # stream, which would undo the low time to first token.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(upstream.aclose),
        )

    return app


def create_app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app(Settings.from_env())
