# Task 1: Customer-support MCP server (stdio)

Python MCP server built on the official `mcp` SDK (2.x, low-level `Server`) with two tools:

| Tool | Arguments |
| --- | --- |
| `get_customer_record` | `customer_id`: `CUST-` followed by exactly 5 digits |
| `trigger_refund` | `customer_id`; `amount`: positive number, max 2 decimals, ≤ 10,000; `reason`: 10–500 chars after trimming |

## Run

```bash
uv run python -m task1_mcp_server.server
```

Inspect it interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run python -m task1_mcp_server.server
```

Test data: `CUST-10001` (refundable balance 500.00) and `CUST-10002` (120.50).

## Error mapping

| Situation | Response |
| --- | --- |
| Arguments fail the schema | JSON-RPC error `-32602` Invalid params, with field-level details in `error.data` |
| Unknown tool name | `-32602` (as in the MCP spec) |
| Unknown method | `-32601` Method not found (SDK) |
| Line is not valid JSON | `-32700` Parse error, `id: null` |
| Valid JSON, not a JSON-RPC message | `-32600` Invalid Request, `id: null` |
| Customer not found, refund above balance | Successful result with `isError: true` |

The last row is deliberate. The MCP spec separates protocol errors (the request itself is wrong) from tool execution errors (the request was fine but the operation failed). Execution errors go back inside the result so the calling model can read them and recover.

## Design notes

- **Validation lives in Pydantic models** (`models.py`) with `strict=True` and `extra="forbid"`. The published `inputSchema` is generated from the same models, so the advertised schema and the enforced one cannot drift.
- **Why the low-level `Server` instead of `MCPServer`:** `MCPServer` reports argument validation failures as a tool result with `isError: true`. The task asks for standard JSON-RPC error codes, so validation runs in the handler and raises `MCPError(INVALID_PARAMS)`.
- **stdout isolation:** logging is configured to stderr before any other import. The SDK's stdio transport also keeps a private duplicate of fd 1 for JSON-RPC and points fd 1 at stderr, so even a stray `print()` cannot corrupt the stream. A test proves this.
- **Malformed lines:** the SDK drops undecodable stdin lines with a debug log. A small adapter between the transport and the server answers them with `-32700` / `-32600` instead.
- **Validation errors do not echo input values back**, only the field, message and error type.

## Assumptions

- `CUST-XXXXX` means five ASCII digits. `[0-9]` is used instead of `\d`, which also matches non-ASCII digits.
- `amount` accepts JSON integers (`10`): JSON has a single number type. Booleans, strings, `NaN` and infinity are rejected.
- Money is a float because the task specifies one. A real system would use integer cents or `Decimal`.

## Tests

```bash
uv run pytest tests/test_task1_mcp_server.py -v
```

The end-to-end tests spawn the server as a subprocess, speak raw JSON-RPC to it, and fail if any stdout line is not a JSON-RPC 2.0 message.
