"""MCP security gateway: authenticates the caller, authorizes tools/call, proxies the rest.

Run with:
    uvicorn task2_mcp_gateway.app:create_app_from_env --factory --port 9000
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from task2_mcp_gateway.auth import AuthError, Principal, authenticate
from task2_mcp_gateway.policy import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    UNAUTHENTICATED,
    Message,
    RpcError,
    authorize,
    parse_message,
)

log = logging.getLogger("task2.gateway")

# Explicit allowlists. The caller's Authorization header is deliberately not
# forwarded: the downstream server should never see, or be able to replay, the
# token that was issued for the gateway.
FORWARD_REQUEST_HEADERS = ("accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")


@dataclass(frozen=True)
class Settings:
    downstream_url: str
    jwt_secret: str
    max_body_bytes: int = 1_000_000
    downstream_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("GATEWAY_JWT_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("GATEWAY_JWT_SECRET must be set to at least 32 characters")
        return cls(
            downstream_url=os.environ.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:9001/mcp"),
            jwt_secret=secret,
        )


def _rpc_error_response(err: RpcError, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(err.body(), status_code=err.http_status, headers=headers)


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _BodyTooLarge
    return bytes(body)


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if client is not None:
            yield
            return
        async with httpx.AsyncClient(timeout=settings.downstream_timeout_s) as owned:
            app.state.client = owned
            yield

    app = FastAPI(title="MCP security gateway", lifespan=lifespan)
    app.state.client = client

    def principal_or_401(request: Request) -> Principal | JSONResponse:
        try:
            return authenticate(request.headers.get("authorization"), settings.jwt_secret)
        except AuthError as exc:
            log.warning("auth rejected: %s", exc)
            err = RpcError(UNAUTHENTICATED, "Invalid or missing bearer token", http_status=401)
            return _rpc_error_response(err, headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'})

    async def forward(request: Request, body: bytes | None, request_id=None) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        if body is not None:
            headers["content-type"] = "application/json"
        upstream_request = app.state.client.build_request(
            request.method, settings.downstream_url, headers=headers, content=body
        )
        try:
            upstream = await app.state.client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            log.error("downstream request failed: %s", type(exc).__name__)
            err = RpcError(INTERNAL_ERROR, "Downstream MCP server unavailable", request_id, http_status=502)
            return _rpc_error_response(err)

        response_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS
        }
        # aiter_bytes decodes any Content-Encoding, so that header is not
        # forwarded. Streaming keeps SSE responses flowing chunk by chunk.
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        principal = principal_or_401(request)
        if isinstance(principal, Response):
            return principal

        try:
            body = await _read_body(request, settings.max_body_bytes)
        except _BodyTooLarge:
            return _rpc_error_response(RpcError(INVALID_REQUEST, "Request body too large", http_status=413))

        try:
            message: Message = parse_message(body)
            authorize(message, principal.role)
        except RpcError as err:
            log.info(
                "deny sub=%s role=%s method=%s tool=%s code=%s",
                principal.subject, principal.role, _method_of(body), _tool_of(body), err.code,
            )
            return _rpc_error_response(err)

        log.info(
            "allow sub=%s role=%s method=%s tool=%s",
            principal.subject, principal.role, message.method, message.tool_name,
        )
        # Forward the message exactly as it was inspected, re-serialized, so the
        # downstream server cannot interpret the raw bytes differently.
        canonical = json.dumps(message.raw, separators=(",", ":"), ensure_ascii=False).encode()
        return await forward(request, canonical, message.request_id)

    @app.get("/mcp")
    @app.delete("/mcp")
    async def mcp_stream_or_close(request: Request) -> Response:
        # GET opens the server-to-client SSE stream; DELETE ends a session.
        # Neither carries a tool call, so authentication is the only check.
        principal = principal_or_401(request)
        if isinstance(principal, Response):
            return principal
        return await forward(request, None)

    return app


def _method_of(body: bytes) -> str | None:
    # Best effort, for the audit log only; the body may be malformed.
    try:
        value = json.loads(body).get("method")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _tool_of(body: bytes) -> str | None:
    try:
        value = json.loads(body)["params"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def create_app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per proxied request is noise
    return create_app(Settings.from_env())
