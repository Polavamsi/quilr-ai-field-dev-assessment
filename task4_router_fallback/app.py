"""LLM gateway endpoint: POST /v1/chat/completions (OpenAI-compatible, non-streaming).

Run with:
    uvicorn task4_router_fallback.app:create_app_from_env --factory --port 8000
"""

import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from task4_router_fallback import errors
from task4_router_fallback.errors import GatewayError, error_body
from task4_router_fallback.limiter import SlidingWindowLimiter
from task4_router_fallback.providers import Provider
from task4_router_fallback.router import ChatRequest, Router

log = logging.getLogger("task4.app")


@dataclass(frozen=True)
class Settings:
    db_path: str
    api_keys: frozenset[str]
    primary: Provider
    secondary: Provider
    tokens_per_minute: int = 50_000

    @classmethod
    def from_env(cls) -> "Settings":
        keys = frozenset(k.strip() for k in os.environ.get("ROUTER_API_KEYS", "").split(",") if k.strip())
        if not keys:
            raise RuntimeError("ROUTER_API_KEYS must list at least one tenant API key")
        return cls(
            db_path=os.environ.get("ROUTER_DB_PATH", "router.db"),
            api_keys=keys,
            tokens_per_minute=int(os.environ.get("ROUTER_TOKENS_PER_MINUTE", "50000")),
            primary=Provider(
                name="primary",
                url=os.environ.get("PRIMARY_URL", "http://127.0.0.1:9101/v1/chat/completions"),
                model=os.environ.get("PRIMARY_MODEL", "primary-model"),
                timeout_s=int(os.environ.get("PRIMARY_TIMEOUT_MS", "3000")) / 1000,
                api_key=os.environ.get("PRIMARY_API_KEY"),
            ),
            secondary=Provider(
                name="secondary",
                url=os.environ.get("SECONDARY_URL", "http://127.0.0.1:9102/v1/chat/completions"),
                model=os.environ.get("SECONDARY_MODEL", "secondary-model"),
                timeout_s=int(os.environ.get("SECONDARY_TIMEOUT_MS", "10000")) / 1000,
                api_key=os.environ.get("SECONDARY_API_KEY"),
            ),
        )


def tenant_id(api_key: str) -> str:
    # Rows in SQLite are keyed by a hash, so the database never holds raw keys.
    return hashlib.sha256(api_key.encode()).hexdigest()[:32]


def _respond(err: GatewayError, request_id: str) -> JSONResponse:
    headers = {"X-Request-Id": request_id}
    if err.retry_after_s is not None:
        headers["Retry-After"] = str(err.retry_after_s)
    return JSONResponse(error_body(err.code, err.message, request_id), status_code=err.status, headers=headers)


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> FastAPI:
    limiter = SlidingWindowLimiter(settings.db_path, limit=settings.tokens_per_minute)
    allowed_tenants = {tenant_id(key) for key in settings.api_keys}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if client is not None:
            yield
            return
        # The client timeout is only a backstop; per-provider deadlines are
        # enforced in call_provider.
        async with httpx.AsyncClient(timeout=30.0) as owned:
            app.state.router = Router(limiter, owned, settings.primary, settings.secondary)
            yield

    app = FastAPI(title="LLM gateway router", lifespan=lifespan)
    if client is not None:
        app.state.router = Router(limiter, client, settings.primary, settings.secondary)
    app.state.limiter = limiter

    def request_id_of(request: Request) -> str:
        if not hasattr(request.state, "request_id"):
            request.state.request_id = uuid.uuid4().hex
        return request.state.request_id

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 body echoes the submitted values. Only field
        # paths are reported here.
        fields = sorted({".".join(str(p) for p in err["loc"] if p != "body") for err in exc.errors()})
        err = GatewayError(400, "invalid_request", f"Invalid request body: {', '.join(fields) or 'malformed JSON'}")
        return _respond(err, request_id_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _respond(GatewayError(exc.status_code, code, "Request could not be routed"), request_id_of(request))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, body: ChatRequest):
        request_id = request_id_of(request)
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        tenant = tenant_id(key.strip()) if scheme.lower() == "bearer" and key.strip() else None
        if tenant not in allowed_tenants:
            return _respond(errors.unauthorized(), request_id)

        try:
            result = await app.state.router.complete(tenant, body, request_id)
        except GatewayError as err:
            return _respond(err, request_id)
        except Exception:
            # Logged in full here; the client gets the generic message only.
            log.exception("unhandled error request_id=%s", request_id)
            return _respond(errors.internal_error(), request_id)
        return JSONResponse(result, headers={"X-Request-Id": request_id})

    return app


def create_app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app(Settings.from_env())
