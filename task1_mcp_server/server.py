"""Customer-support MCP server over stdio.

stdout carries JSON-RPC frames only. Every log line goes to stderr.
"""

import contextlib
import logging
import sys

# Configure logging before anything else imports and logs, so no handler
# ever gets attached to stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from task1_mcp_server.models import GetCustomerRecordArgs, TriggerRefundArgs
from task1_mcp_server.store import CustomerNotFound, CustomerStore, RefundExceedsBalance

log = logging.getLogger("task1.server")

TOOLS: dict[str, tuple[type, str]] = {
    "get_customer_record": (
        GetCustomerRecordArgs,
        "Look up a customer by ID (format CUST-XXXXX, five digits).",
    ),
    "trigger_refund": (
        TriggerRefundArgs,
        "Refund a positive amount to a customer. Reason must be at least 10 characters.",
    ),
}

store = CustomerStore()


def _validation_details(exc: ValidationError) -> list[dict]:
    # Field path, message and error type only. Input values are left out so
    # bad data is not echoed back to the caller.
    return [
        {"field": ".".join(str(p) for p in err["loc"]) or "(root)", "message": err["msg"], "type": err["type"]}
        for err in exc.errors(include_input=False, include_url=False)
    ]


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=desc, input_schema=model.model_json_schema())
            for name, (model, desc) in TOOLS.items()
        ]
    )


def _text(payload: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=payload)], is_error=is_error)


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    entry = TOOLS.get(params.name)
    if entry is None:
        raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {params.name}")

    model, _ = entry
    try:
        args = model.model_validate(params.arguments or {})
    except ValidationError as exc:
        log.info("rejected %s: %d validation error(s)", params.name, exc.error_count())
        raise MCPError(
            code=types.INVALID_PARAMS,
            message=f"Invalid arguments for tool {params.name}",
            data={"errors": _validation_details(exc)},
        ) from None

    # Business failures (unknown customer, refund too large) are tool
    # execution errors, not protocol errors: the call itself was valid, so
    # they come back as a result with isError=true for the model to read.
    try:
        if isinstance(args, GetCustomerRecordArgs):
            record = store.get(args.customer_id)
            return _text(record.model_dump_json())
        refund = store.refund(args.customer_id, args.amount, args.reason)
        return _text(refund.model_dump_json())
    except (CustomerNotFound, RefundExceedsBalance) as exc:
        return _text(str(exc), is_error=True)


server = Server("customer-support", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)


def _framing_error(exc: Exception) -> types.JSONRPCError:
    # The SDK hands us the exception from decoding a stdin line. Bad JSON is a
    # parse error; valid JSON that is not a JSON-RPC message is an invalid
    # request. The id is unknown in both cases, so it is null per JSON-RPC 2.0.
    is_parse_error = isinstance(exc, ValidationError) and any(
        err["type"] == "json_invalid" for err in exc.errors()
    )
    if is_parse_error:
        error = types.ErrorData(code=types.PARSE_ERROR, message="Parse error")
    else:
        error = types.ErrorData(code=types.INVALID_REQUEST, message="Invalid Request")
    return types.JSONRPCError(jsonrpc="2.0", id=None, error=error)


async def _reject_malformed(transport_read, server_send, write_stream) -> None:
    # The SDK drops undecodable lines with only a debug log. This sits between
    # the transport and the server and answers them with a JSON-RPC error.
    async with server_send:
        async for item in transport_read:
            if isinstance(item, Exception):
                log.warning("malformed message on stdin: %s", type(item).__name__)
                await write_stream.send(SessionMessage(_framing_error(item)))
                continue
            await server_send.send(item)


async def main() -> None:
    log.info("starting customer-support MCP server on stdio")
    async with stdio_server() as (transport_read, write_stream):
        # The transport now holds its own handle on the real stdout. Point
        # sys.stdout at stderr so a stray print() in any handler goes to stderr
        # right away. The SDK alone is not enough: it only diverts fd 1 while
        # serving, and buffered print output can be flushed to the real stdout
        # after it restores fd 1 on shutdown.
        with contextlib.redirect_stdout(sys.stderr):
            server_send, server_read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            async with anyio.create_task_group() as tg:
                tg.start_soon(_reject_malformed, transport_read, server_send, write_stream)
                await server.run(server_read, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
