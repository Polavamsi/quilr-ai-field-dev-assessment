# Task 2: MCP security gateway

A reverse proxy in front of an MCP server (Streamable HTTP). It authenticates the caller from a bearer token, inspects every JSON-RPC message, blocks `admin_*` tool calls for non-admins, and proxies everything else.

```
AI agent ──Bearer JWT──▶ gateway :9000 /mcp ──(no token)──▶ downstream MCP server :9001 /mcp
```

## Run

Three terminals, from the repo root:

```bash
# 1. downstream mock MCP server
uv run uvicorn task2_mcp_gateway.mock_downstream:app --port 9001

# 2. gateway
export GATEWAY_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
uv run uvicorn task2_mcp_gateway.app:create_app_from_env --factory --port 9000

# 3. client (same GATEWAY_JWT_SECRET exported)
VIEWER=$(uv run python -m task2_mcp_gateway.mint_token --role viewer --sub bob)
curl -s localhost:9000/mcp \
  -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
# {"jsonrpc":"2.0","id":2,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

`GATEWAY_DOWNSTREAM_URL` overrides the downstream address (default `http://127.0.0.1:9001/mcp`).

## Request pipeline

1. **Authenticate.** `Authorization: Bearer <JWT>`, HS256 only, `exp`/`sub`/`role` required, role in `{admin, viewer}`. Failure: HTTP 401, `WWW-Authenticate: Bearer`, JSON-RPC error `-32000`. The body is never read for unauthenticated callers.
2. **Read the body** with a 1 MB cap (HTTP 413 above it).
3. **Parse** as a single JSON-RPC 2.0 message (see below). Failure: HTTP 400 with `-32700` or `-32600`.
4. **Authorize.** `tools/call` with a `params.name` starting with `admin_` requires role `admin`. Failure: `-32001 Unauthorized Tool Call` with the request's `id`, and the downstream server is never called.
5. **Forward** the message re-serialized from the parsed object, with an allowlist of headers, and stream the response back (JSON or SSE).

| Situation | HTTP | JSON-RPC |
| --- | --- | --- |
| Missing, malformed, expired, unsigned or wrong-secret token | 401 | `-32000` |
| Body over 1 MB | 413 | `-32600` |
| Not JSON, or `NaN`/`Infinity` literals | 400 | `-32700` |
| Batch array, duplicate keys, bad envelope, `tools/call` without `id` | 400 | `-32600` |
| `tools/call` without a string `params.name` | 200 | `-32602` |
| Non-admin calls an `admin_*` tool | 200 | `-32001` |
| Downstream unreachable | 502 | `-32603` (no hostnames or exception text) |

## Bypasses this closes

- **Batches.** `[{"method":"tools/call","params":{"name":"admin_reset_key"}}]` slips past a check that only inspects objects. MCP removed batching in protocol version 2025-06-18, so batches are rejected.
- **Duplicate keys.** `{"name":"get_customer_record","name":"admin_reset_key"}`: Python keeps the last value, some parsers keep the first, so the gateway could approve one tool while the server runs another. Duplicate keys are rejected, and the downstream server receives the re-serialized object the gateway inspected, never the raw bytes.
- **Look-alike names.** `Admin_reset_key`, fullwidth `ａｄｍｉｎ_reset_key`, a leading space, or a zero-width character. Names are NFKC-normalized, stripped of format characters and whitespace, and case-folded before the prefix check. Over-matching only ever denies; it never grants.
- **Notification form.** A `tools/call` without an `id` gets no response under JSON-RPC, so a denial could not be reported. It is rejected as an invalid request.
- **Token passthrough.** The caller's `Authorization` header is not forwarded. The MCP authorization spec forbids passing client tokens through to downstream servers.

## Design notes

- `policy.py` is pure (bytes in, decision out) and `auth.py` only verifies tokens, so the security logic is unit-tested without HTTP.
- JSON-RPC errors for valid requests use HTTP 200, as an MCP server itself would; MCP clients read the error from the body. Malformed input uses 400, per the Streamable HTTP transport.
- `tools/list` is forwarded unchanged, as the task specifies. A natural extension is filtering `admin_*` tools out of the list for viewers so agents never see tools they cannot call.
- `GET /mcp` (SSE stream) and `DELETE /mcp` (end session) are authenticated and proxied; they carry no tool calls.
- Each decision is logged as `allow`/`deny` with subject, role, method and tool. Tokens are never logged.

## Tests

```bash
uv run pytest tests/test_task2_policy.py tests/test_task2_gateway.py -v
```

The gateway tests run the real gateway against the mock server in-process and assert, for every rejection, that the downstream server received nothing.
