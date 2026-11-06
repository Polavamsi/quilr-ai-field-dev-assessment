import json
import time

import httpx
import jwt
import pytest

from task2_mcp_gateway.app import Settings, create_app
from task2_mcp_gateway.mint_token import mint
from task2_mcp_gateway.mock_downstream import create_mock_app

SECRET = "test-secret-that-is-at-least-32-characters"
ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def downstream():
    return create_mock_app()


@pytest.fixture
async def gateway(downstream):
    to_downstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=downstream), base_url="http://downstream")
    app = create_app(Settings(downstream_url="http://downstream/mcp", jwt_secret=SECRET), client=to_downstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        yield client
    await to_downstream.aclose()


def bearer(role="viewer", sub="bob", **overrides):
    return {"Authorization": f"Bearer {overrides.pop('token', None) or mint(SECRET, role, sub)}", "Accept": ACCEPT}


def rpc(method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def tool_calls_received(downstream):
    return [r["message"] for r in downstream.state.received if r["message"].get("method") == "tools/call"]


# ---------- authentication ----------

def _expired():
    now = int(time.time())
    return jwt.encode({"sub": "bob", "role": "viewer", "iat": now - 120, "exp": now - 60}, SECRET, algorithm="HS256")


def _unsigned():
    return jwt.encode({"sub": "bob", "role": "admin", "exp": int(time.time()) + 60}, None, algorithm="none")


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-jwt",
        f"Bearer {mint('a-different-secret-that-is-also-32-chars', 'admin', 'eve')}",
        f"Bearer {_expired()}",
        f"Bearer {_unsigned()}",
        f"Bearer {jwt.encode({'sub': 'bob', 'role': 'superuser', 'exp': int(time.time()) + 60}, SECRET)}",
        f"Bearer {jwt.encode({'sub': 'bob', 'exp': int(time.time()) + 60}, SECRET)}",
    ],
    ids=["missing", "empty", "no-token", "basic", "garbage", "wrong-secret", "expired", "alg-none", "unknown-role", "no-role"],
)
async def test_bad_credentials_get_401_and_never_reach_downstream(gateway, downstream, authorization):
    headers = {"Accept": ACCEPT}
    if authorization is not None:
        headers["Authorization"] = authorization
    resp = await gateway.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert resp.json()["error"]["code"] == -32000
    assert downstream.state.received == []


async def test_get_and_delete_also_require_auth(gateway):
    assert (await gateway.get("/mcp")).status_code == 401
    assert (await gateway.delete("/mcp")).status_code == 401


async def test_lowercase_bearer_scheme_accepted(gateway):
    headers = bearer()
    headers["Authorization"] = headers["Authorization"].replace("Bearer", "bearer")
    resp = await gateway.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert resp.status_code == 200


# ---------- authorization ----------

async def test_tools_list_forwarded_transparently(gateway, downstream):
    resp = await gateway.post("/mcp", json=rpc("tools/list"), headers=bearer("viewer"))
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {"get_customer_record", "admin_reset_key"}
    assert downstream.state.received[0]["message"]["method"] == "tools/list"


async def test_viewer_can_call_normal_tool(gateway):
    resp = await gateway.post("/mcp", json=rpc("tools/call", {"name": "get_customer_record"}), headers=bearer("viewer"))
    assert resp.json()["result"]["content"][0]["text"] == "get_customer_record executed"


@pytest.mark.parametrize("name", ["admin_reset_key", "Admin_reset_key", "ａｄｍｉｎ_reset_key"])
async def test_viewer_blocked_from_admin_tool(gateway, downstream, name):
    resp = await gateway.post("/mcp", json=rpc("tools/call", {"name": name}, req_id="req-42"), headers=bearer("viewer"))
    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": "req-42", "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert tool_calls_received(downstream) == []


async def test_admin_can_call_admin_tool(gateway, downstream):
    resp = await gateway.post("/mcp", json=rpc("tools/call", {"name": "admin_reset_key"}), headers=bearer("admin"))
    assert resp.json()["result"]["content"][0]["text"] == "admin_reset_key executed"
    assert tool_calls_received(downstream)[0]["params"]["name"] == "admin_reset_key"


@pytest.mark.parametrize(
    "body",
    [
        b'[{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}]',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_customer_record","name":"admin_reset_key"}}',
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"admin_reset_key"}}',
    ],
    ids=["batch", "duplicate-key", "notification"],
)
async def test_smuggling_attempts_rejected_before_downstream(gateway, downstream, body):
    headers = {**bearer("viewer"), "Content-Type": "application/json"}
    resp = await gateway.post("/mcp", content=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600
    assert downstream.state.received == []


async def test_malformed_json_is_parse_error(gateway, downstream):
    resp = await gateway.post("/mcp", content=b"{oops", headers={**bearer(), "Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert downstream.state.received == []


async def test_oversized_body_rejected(gateway, downstream):
    big = rpc("tools/call", {"name": "get_customer_record", "arguments": {"pad": "x" * 1_100_000}})
    resp = await gateway.post("/mcp", json=big, headers=bearer())
    assert resp.status_code == 413
    assert downstream.state.received == []


# ---------- proxying ----------

async def test_token_not_forwarded_but_session_headers_are(gateway, downstream):
    init = await gateway.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-11-25"}), headers=bearer())
    session_id = init.headers["mcp-session-id"]
    assert session_id

    headers = {**bearer(), "Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-11-25", "X-Internal": "1"}
    await gateway.post("/mcp", json=rpc("tools/list", req_id=2), headers=headers)

    seen = downstream.state.received[-1]["headers"]
    assert "authorization" not in seen
    assert "x-internal" not in seen
    assert seen["mcp-session-id"] == session_id
    assert seen["mcp-protocol-version"] == "2025-11-25"


async def test_downstream_receives_exactly_what_was_authorized(gateway, downstream):
    raw = b'{"jsonrpc": "2.0",   "id": 9, "method": "tools/call", "params": {"name": "get_customer_record"}}'
    await gateway.post("/mcp", content=raw, headers={**bearer(), "Content-Type": "application/json"})
    assert downstream.state.received[0]["message"] == json.loads(raw)


async def test_notifications_forwarded(gateway, downstream):
    resp = await gateway.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=bearer())
    assert resp.status_code == 202
    assert downstream.state.received[0]["message"]["method"] == "notifications/initialized"


async def test_sse_responses_streamed_through(gateway):
    headers = {**bearer(), "Accept": "text/event-stream"}
    resp = await gateway.post("/mcp", json=rpc("tools/call", {"name": "get_customer_record"}), headers=headers)
    assert resp.headers["content-type"].startswith("text/event-stream")
    data_line = next(line for line in resp.text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line[len("data: "):])["result"]["isError"] is False


async def test_downstream_down_returns_502_without_internals():
    def refuse(request):
        raise httpx.ConnectError("connection refused to 10.0.0.5:9001")

    dead = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    app = create_app(Settings(downstream_url="http://downstream/mcp", jwt_secret=SECRET), client=dead)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        resp = await client.post("/mcp", json=rpc("tools/list", req_id=3), headers=bearer())
    assert resp.status_code == 502
    assert resp.json()["id"] == 3
    assert resp.json()["error"]["code"] == -32603
    assert "10.0.0.5" not in resp.text
