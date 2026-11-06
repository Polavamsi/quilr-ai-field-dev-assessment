"""Stand-in OpenAI-compatible provider with a selectable failure mode.

    MOCK_NAME=primary MOCK_BEHAVIOR=rate_limited uvicorn task4_router_fallback.mock_provider:app --port 9101

Behaviors: ok, rate_limited, slow, server_error, garbage.
"""

import asyncio
import math
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

SLOW_SECONDS = 5.0


def completion_body(model: str, messages: list[dict], text: str) -> dict:
    prompt_tokens = sum(math.ceil(len(m.get("content", "")) / 4) + 4 for m in messages)
    completion_tokens = math.ceil(len(text) / 4)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def create_mock_provider(name: str, behavior: str) -> FastAPI:
    app = FastAPI(title=f"Mock provider ({name})")

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        payload = await request.json()
        if behavior == "rate_limited":
            return JSONResponse({"error": {"message": "Rate limit reached for org-internal-7731"}}, status_code=429)
        if behavior == "server_error":
            return PlainTextResponse("Traceback (most recent call last):\n  File \"/srv/llm/worker.py\"", status_code=500)
        if behavior == "garbage":
            return PlainTextResponse("<html>upstream proxy error</html>", status_code=200)
        if behavior == "slow":
            await asyncio.sleep(SLOW_SECONDS)
        return completion_body(payload.get("model", name), payload.get("messages", []), f"Hello from {name}.")

    return app


app = create_mock_provider(os.environ.get("MOCK_NAME", "mock"), os.environ.get("MOCK_BEHAVIOR", "ok"))
