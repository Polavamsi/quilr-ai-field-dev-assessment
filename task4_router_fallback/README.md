# Task 4: Token-aware rate limiter and model fallback router

An OpenAI-compatible `POST /v1/chat/completions` endpoint that:

1. identifies the tenant from its API key,
2. reserves tokens against a **50,000 tokens/minute sliding window** per tenant, stored in an on-disk SQLite file,
3. calls the **primary** model, and fails over to the **secondary** on HTTP 429, a **3000 ms timeout**, a 5xx, a connection error or an unusable response,
4. corrects the reservation to the provider's reported `usage.total_tokens`,
5. returns every error as `{"error": {"code", "message", "request_id"}}` with nothing copied from upstream.

## Run

Four terminals, from the repo root:

```bash
# providers: try MOCK_BEHAVIOR=rate_limited or slow on the primary to see failover
MOCK_NAME=primary   MOCK_BEHAVIOR=ok uv run uvicorn task4_router_fallback.mock_provider:app --port 9101
MOCK_NAME=secondary MOCK_BEHAVIOR=ok uv run uvicorn task4_router_fallback.mock_provider:app --port 9102

# router
ROUTER_API_KEYS=demo-key uv run uvicorn task4_router_fallback.app:create_app_from_env --factory --port 8000

# client
curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer demo-key" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'
```

| Variable | Default |
| --- | --- |
| `ROUTER_API_KEYS` | required, comma-separated tenant keys |
| `ROUTER_DB_PATH` | `router.db` |
| `ROUTER_TOKENS_PER_MINUTE` | `50000` |
| `PRIMARY_URL` / `SECONDARY_URL` | `http://127.0.0.1:9101/...` / `:9102/...` |
| `PRIMARY_TIMEOUT_MS` / `SECONDARY_TIMEOUT_MS` | `3000` / `10000` |
| `PRIMARY_MODEL`, `SECONDARY_MODEL`, `PRIMARY_API_KEY`, `SECONDARY_API_KEY` | optional |

## Rate limiter

**Algorithm: sliding log.** Every admitted request is a row `(tenant, ts, tokens)`. Usage is the sum of the tenant's rows newer than 60 seconds. A fixed-window counter allows up to 2× the limit across a window boundary; a log has no boundary. At 50k tokens per minute the number of rows per tenant stays small.

**Token accounting.** The real cost of a completion is unknown until it finishes, so the router reserves a pessimistic estimate first and corrects it afterwards:

- estimate = `ceil(chars / 4) + 4` per message, plus `max_tokens` (512 if unset);
- on success the row is updated to the provider's `usage.total_tokens`;
- if every provider fails, the row is deleted, because nothing was produced.

Overestimating only delays a tenant briefly until reconciliation. Underestimating would let a tenant run past its limit while requests are in flight. A real deployment would swap the character heuristic for the model's tokenizer; the reserve-then-reconcile flow stays the same.

**Concurrency.** Reading usage and inserting the reservation run in one `BEGIN IMMEDIATE` transaction, which takes SQLite's write lock before the read. This holds across threads and across processes that share the file. A test races 40 threads at 5,000 tokens each against the 50,000 limit and requires exactly 10 to win; with the transaction removed, it fails. WAL mode keeps readers unblocked, and `busy_timeout` makes writers wait for the lock instead of erroring. SQLite calls run in `asyncio.to_thread` so they never block the event loop.

**Eviction.** Each reservation deletes every row older than the window (an indexed range delete on `ts`), so the table holds at most one minute of history.

**Denials** return HTTP 429 with `Retry-After`, computed as when enough of the tenant's oldest rows expire to fit the request. A single request whose estimate exceeds the whole per-minute limit can never succeed, so it gets HTTP 400 `request_too_large` instead of an endless 429.

**Tenant identity.** Rows are keyed by a SHA-256 hash of the API key, so the database never contains raw keys.

## Fallback

| Primary outcome | Action |
| --- | --- |
| 2xx with a valid body | return it |
| 429, no response within 3000 ms, 5xx, connection error, non-JSON or missing `choices` | try the secondary |
| other 4xx | no failover: the secondary would reject the same request; return 502 `upstream_error` |

The 3000 ms deadline is `asyncio.timeout` around the entire exchange (connect, send, wait, read body). On expiry the in-flight request is cancelled, which closes its connection, so a hung primary does not keep holding a socket. A test confirms the abandoned call is actually cancelled and that failover happens at the deadline, not when the slow upstream finally answers.

## Errors

| Situation | HTTP | `code` |
| --- | --- | --- |
| Missing or unknown API key | 401 | `invalid_api_key` |
| Body fails validation, malformed JSON, `stream: true` | 400 | `invalid_request` (field names only, never values) |
| One request larger than the per-minute limit | 400 | `request_too_large` |
| Tenant over its limit | 429 + `Retry-After` | `rate_limited` |
| Primary refused the request (4xx) | 502 | `upstream_error` |
| Both providers failed | 503 | `upstream_unavailable` |
| Anything unexpected | 500 | `internal_error` |

Messages are fixed strings. Upstream bodies, provider names, hostnames, exception text and stack traces go to the server log with the `request_id`, never to the client. Every response, success or error, carries `X-Request-Id` for correlating a client report with the logs.

## Limitations

- Non-streaming only (`stream: true` is rejected). With streaming, failover is only possible before the first token is sent, and usage is reconciled from the final chunk.
- A primary that answers just after the deadline has still done the work; that cost is not charged to the tenant.
- One SQLite file suits one host. Several hosts would share a Redis sorted set or a database with the same reserve-then-reconcile logic.

## Tests

```bash
uv run pytest tests/test_task4_limiter.py tests/test_task4_router.py -v
```
