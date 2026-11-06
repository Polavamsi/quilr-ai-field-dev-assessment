"""Mock OpenAI-compatible model that streams a reply containing PII.

The reply is cut into 1 to 6 character pieces at random, so emails, SSNs and
card numbers regularly straddle chunk boundaries.

    MOCK_CHUNK_DELAY_MS=20 uvicorn task3_pii_stream_guardrail.mock_llm:app --port 9201
"""

import asyncio
import json
import os
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DEMO_REPLY = (
    "Sure. The account owner is Jane Doe, email jane.doe+billing@example.co.uk, "
    "SSN 123-45-6789. The card on file is 4111 1111 1111 1111 and the backup is "
    "5500-0000-0000-0004. Order 1234567890123 shipped on 2026-11-05. "
    "Questions go to support@example.com."
)


def split_reply(text: str, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    pieces, i = [], 0
    while i < len(text):
        size = rng.randint(1, 6)
        pieces.append(text[i:i + size])
        i += size
    return pieces


def create_mock_llm(reply: str = DEMO_REPLY, chunk_delay_s: float = 0.02, repeat: int = 1) -> FastAPI:
    app = FastAPI(title="Mock streaming LLM")

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        model = body.get("model") or "mock-model"
        text = reply * repeat
        if not body.get("stream"):
            return JSONResponse({
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            })

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        def chunk(delta: dict, finish_reason=None) -> str:
            payload = {
                "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        async def events():
            yield chunk({"role": "assistant", "content": ""})
            for piece in split_reply(text, seed=len(text)):
                await asyncio.sleep(chunk_delay_s)
                yield chunk({"content": piece})
            yield chunk({}, finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


app = create_mock_llm(
    chunk_delay_s=float(os.environ.get("MOCK_CHUNK_DELAY_MS", "20")) / 1000,
    repeat=int(os.environ.get("MOCK_REPEAT", "1")),
)
