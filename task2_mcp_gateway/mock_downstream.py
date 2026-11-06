"""Minimal downstream MCP server over Streamable HTTP, for demos and tests.

Run with:
    uvicorn task2_mcp_gateway.mock_downstream:app --port 9001
"""

import json
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Look up a customer by ID.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a customer's API key. Admin only.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def create_mock_app() -> FastAPI:
    app = FastAPI(title="Mock downstream MCP server")
    # Every request the mock receives, so tests can assert what reached it.
    app.state.received = []

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        msg = await request.json()
        app.state.received.append({"message": msg, "headers": dict(request.headers)})

        if "id" not in msg:
            return Response(status_code=202)  # notification or client response

        headers = {}
        method = msg.get("method")
        if method == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
            payload = {
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-downstream", "version": "0.1.0"},
                }
            }
        elif method == "tools/list":
            payload = {"result": {"tools": TOOLS}}
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name in TOOL_NAMES:
                payload = {"result": {"content": [{"type": "text", "text": f"{name} executed"}], "isError": False}}
            else:
                payload = {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        elif method == "ping":
            payload = {"result": {}}
        else:
            payload = {"error": {"code": -32601, "message": "Method not found"}}

        body = {"jsonrpc": "2.0", "id": msg["id"], **payload}
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept and "application/json" not in accept:
            sse = f"event: message\ndata: {json.dumps(body)}\n\n"
            return Response(sse, media_type="text/event-stream", headers=headers)
        return JSONResponse(body, headers=headers)

    return app


app = create_mock_app()
