import asyncio
import json
import time

import httpx
import pytest

from task4_router_fallback.app import Settings, create_app, tenant_id
from task4_router_fallback.mock_provider import completion_body
from task4_router_fallback.providers import Provider

API_KEY = "tenant-key-alpha"
PRIMARY = Provider(name="primary", url="http://primary/v1/chat/completions", model="p-model", timeout_s=0.3)
SECONDARY = Provider(name="secondary", url="http://secondary/v1/chat/completions", model="s-model", timeout_s=1.0)
# Short deadlines keep the suite fast; one test below checks the real 3000 ms setting.


class Upstreams:
    """Fake providers selected by host. Records calls and notices cancellation."""

    def __init__(self, primary="ok", secondary="ok", slow_s=5.0):
        self.behavior = {"primary": primary, "secondary": secondary}
        self.slow_s = slow_s
        self.calls: list[str] = []
        self.cancelled: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        payload = json.loads(request.content)
        behavior = self.behavior[host]
        if behavior == "rate_limited":
            return httpx.Response(429, json={"error": {"message": "quota exceeded for org-internal-7731"}})
        if behavior == "server_error":
            return httpx.Response(500, text='Traceback (most recent call last):\n  File "/srv/llm/worker.py"')
        if behavior == "bad_request":
            return httpx.Response(400, json={"error": {"message": "context length exceeded"}})
        if behavior == "garbage":
            return httpx.Response(200, text="<html>proxy error</html>")
        if behavior == "connect_error":
            raise httpx.ConnectError("connection refused: 10.0.3.17:443")
        if behavior == "slow":
            try:
                await asyncio.sleep(self.slow_s)
            except asyncio.CancelledError:
                self.cancelled.append(host)
                raise
        return httpx.Response(200, json=completion_body(payload["model"], payload["messages"], f"hi from {host}"))


def make_settings(tmp_path, limit=50_000, primary=PRIMARY):
    return Settings(
        db_path=str(tmp_path / "router.db"),
        api_keys=frozenset({API_KEY, "tenant-key-beta"}),
        primary=primary,
        secondary=SECONDARY,
        tokens_per_minute=limit,
    )


@pytest.fixture
def upstreams():
    return Upstreams()


@pytest.fixture
async def gateway(tmp_path, upstreams):
    to_providers = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handler))
    app = create_app(make_settings(tmp_path), client=to_providers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        client.app = app
        yield client
    await to_providers.aclose()


def chat(content="Hello there", max_tokens=100, **extra):
    return {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **extra}


AUTH = {"Authorization": f"Bearer {API_KEY}"}


def assert_sanitized(resp, status, code):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == resp.headers["x-request-id"]
    for leak in ("Traceback", "worker.py", "org-internal", "10.0.3.17", "primary", "secondary", "p-model"):
        assert leak not in resp.text


# ---------- routing and fallback ----------

async def test_primary_serves_when_healthy(gateway, upstreams):
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi from primary"
    assert upstreams.calls == ["primary"]


@pytest.mark.parametrize("failure", ["rate_limited", "slow", "server_error", "connect_error", "garbage"])
async def test_fails_over_to_secondary(gateway, upstreams, failure):
    upstreams.behavior["primary"] = failure
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi from secondary"
    assert upstreams.calls == ["primary", "secondary"]


async def test_timeout_cancels_primary_and_fails_over_promptly(gateway, upstreams):
    upstreams.behavior["primary"] = "slow"
    start = time.monotonic()
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    assert upstreams.cancelled == ["primary"]   # the abandoned call was actually cancelled
    assert 0.3 <= elapsed < 1.0                  # waited for the deadline, not for the slow upstream


async def test_primary_client_error_does_not_fail_over(gateway, upstreams):
    upstreams.behavior["primary"] = "bad_request"
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert_sanitized(resp, 502, "upstream_error")
    assert upstreams.calls == ["primary"]


@pytest.mark.parametrize(
    "primary,secondary", [("rate_limited", "server_error"), ("slow", "slow"), ("connect_error", "garbage")]
)
async def test_both_providers_down_returns_sanitized_503(gateway, upstreams, primary, secondary):
    upstreams.behavior.update(primary=primary, secondary=secondary)
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert_sanitized(resp, 503, "upstream_unavailable")


async def test_failed_request_is_not_charged(gateway, upstreams):
    upstreams.behavior.update(primary="server_error", secondary="server_error")
    await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert gateway.app.state.limiter.usage(tenant_id(API_KEY)) == 0


async def test_usage_is_reconciled_to_actual_tokens(gateway):
    resp = await gateway.post("/v1/chat/completions", json=chat(max_tokens=4000), headers=AUTH)
    actual = resp.json()["usage"]["total_tokens"]
    assert actual < 100                          # reservation was ~4,007 tokens
    assert gateway.app.state.limiter.usage(tenant_id(API_KEY)) == actual


def test_default_primary_timeout_is_3000ms(monkeypatch):
    monkeypatch.setenv("ROUTER_API_KEYS", "k")
    monkeypatch.delenv("PRIMARY_TIMEOUT_MS", raising=False)
    assert Settings.from_env().primary.timeout_s == 3.0


# ---------- rate limiting ----------

async def test_rate_limit_returns_429_with_retry_after_and_skips_providers(tmp_path, upstreams):
    to_providers = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handler))
    app = create_app(make_settings(tmp_path, limit=1_000), client=to_providers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        app.state.limiter.reserve(tenant_id(API_KEY), 990)  # tenant has 10 tokens left this minute

        resp = await client.post("/v1/chat/completions", json=chat(max_tokens=50), headers=AUTH)
        assert_sanitized(resp, 429, "rate_limited")
        assert 1 <= int(resp.headers["retry-after"]) <= 60
        assert upstreams.calls == []

        other = await client.post(
            "/v1/chat/completions", json=chat(max_tokens=50), headers={"Authorization": "Bearer tenant-key-beta"}
        )
        assert other.status_code == 200  # limits are per tenant
    await to_providers.aclose()


async def test_request_larger_than_limit_is_400(gateway):
    resp = await gateway.post("/v1/chat/completions", json=chat(content="x" * 200_000, max_tokens=8192), headers=AUTH)
    assert_sanitized(resp, 400, "request_too_large")


# ---------- input and auth errors ----------

@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-key"}, {"Authorization": f"Basic {API_KEY}"}, {"Authorization": "Bearer"}]
)
async def test_bad_api_key_is_401(gateway, upstreams, headers):
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=headers)
    assert_sanitized(resp, 401, "invalid_api_key")
    assert upstreams.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "hacker", "content": "SECRET-INPUT-VALUE"}]},
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 0},
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": "SECRET-INPUT-VALUE"},
        {"messages": [{"role": "user", "content": "hi"}], "stream": True},
        {"messages": [{"role": "user", "content": "hi"}], "tools": ["SECRET-INPUT-VALUE"]},
    ],
)
async def test_invalid_body_is_400_without_echoing_input(gateway, upstreams, body):
    resp = await gateway.post("/v1/chat/completions", json=body, headers=AUTH)
    assert_sanitized(resp, 400, "invalid_request")
    assert "SECRET-INPUT-VALUE" not in resp.text
    assert upstreams.calls == []


async def test_malformed_json_is_400(gateway):
    resp = await gateway.post(
        "/v1/chat/completions", content=b"{nope", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert_sanitized(resp, 400, "invalid_request")


async def test_unknown_route_uses_standard_error_shape(gateway):
    assert_sanitized(await gateway.get("/admin/debug"), 404, "not_found")


async def test_unexpected_exception_is_sanitized(gateway, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("db password is hunter2 at /etc/router/secrets.yaml")

    monkeypatch.setattr(gateway.app.state.router, "complete", explode)
    resp = await gateway.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert_sanitized(resp, 500, "internal_error")
    assert "hunter2" not in resp.text and "secrets.yaml" not in resp.text
