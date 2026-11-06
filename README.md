# QuilrAI AI Field Developer assessment

Four components of an AI security gateway, in Python 3.11+, each with its own README, a mock upstream (no API keys needed) and tests.

| Task | Folder | What it does |
| --- | --- | --- |
| 1 | [`task1_mcp_server`](task1_mcp_server/README.md) | MCP server over stdio with two strictly validated tools and standard JSON-RPC errors; stdout carries nothing but JSON-RPC |
| 2 | [`task2_mcp_gateway`](task2_mcp_gateway/README.md) | MCP reverse proxy: JWT bearer auth, `admin_*` tool calls blocked for non-admins with `-32001`, everything else streamed through |
| 3 | [`task3_pii_stream_guardrail`](task3_pii_stream_guardrail/README.md) | LLM streaming proxy that redacts emails, SSNs and card numbers as tokens arrive, including PII split across chunks |
| 4 | [`task4_router_fallback`](task4_router_fallback/README.md) | 50,000 tokens/minute sliding-window limiter per tenant in SQLite, with failover to a secondary model on 429 or a 3000 ms timeout |

## Setup

Requires [uv](https://docs.astral.sh/uv/). `uv.lock` pins the exact versions tested.

```bash
uv sync
uv run pytest
```

234 tests, about 10 seconds. Tested on macOS and Linux with Python 3.12. Each task README has run instructions, design notes and assumptions.

## Highlights

**Task 1.** Uses the low-level `Server` from the MCP Python SDK 2.x, because its higher-level `MCPServer` reports argument validation failures as tool results rather than JSON-RPC `-32602` errors. Undecodable stdin lines get `-32700`/`-32600` instead of being dropped. A test makes a tool handler `print()` and checks the output lands on stderr. That test caught a macOS-only leak: buffered output flushed to stdout after the SDK restored file descriptor 1 at shutdown.

**Task 2.** Closes bypasses a naive prefix check misses: JSON-RPC batch arrays, duplicate JSON keys (the gateway and the server may pick different values), case and Unicode look-alikes of `admin_`, and `tools/call` sent as a notification. The downstream server receives the re-serialized message the gateway inspected, never the raw bytes, and never the client's token.

**Task 3.** Holds back only the text that could still become PII: partial regex matches, and complete matches that could still grow, which is usually the last word. Output is identical however the stream is chunked, verified at every split point and by a fuzzer comparing streamed and one-shot redaction. Memory per stream is capped at 540 characters. Measured overhead: +24 ms time to first token (one upstream chunk interval), +0.3% total stream time.

**Task 4.** Reserves an estimated token count before calling the model and corrects it to the provider's reported usage afterwards. The check-and-reserve step runs in one `BEGIN IMMEDIATE` transaction; a 40-thread test admits exactly 10 requests of 5,000 tokens against the 50,000 limit, and fails if the transaction is removed. The 3000 ms deadline covers the whole upstream exchange and cancels the abandoned request. Every error is a fixed `{code, message, request_id}`; tests plant stack traces and hostnames in upstream failures and check none reach the client.

## Layout

```
task1_mcp_server/            server.py, models.py (Pydantic schemas), store.py
task2_mcp_gateway/           app.py, policy.py (parse + authorize), auth.py (JWT), mock_downstream.py, mint_token.py
task3_pii_stream_guardrail/  redactor.py (holdback algorithm), sse.py, app.py, mock_llm.py, bench.py
task4_router_fallback/       limiter.py (SQLite), router.py, providers.py, errors.py, tokens.py, app.py, mock_provider.py
tests/                       one or two test files per task
```
