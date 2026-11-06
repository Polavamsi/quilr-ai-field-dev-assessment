"""Redaction applied to an OpenAI-style chat completion SSE stream.

Input is the upstream stream as text lines; output is SSE events in the same
format with every `choices[i].delta.content` redacted. Each choice index gets
its own StreamRedactor, so responses with n > 1 do not mix their text.
"""

import copy
import json
import logging
from collections.abc import AsyncIterator

from task3_pii_stream_guardrail.redactor import StreamRedactor

log = logging.getLogger("task3.sse")

DONE = "data: [DONE]\n\n"


def sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n"


def stream_error_event(code: str, message: str) -> str:
    return sse_event({"error": {"code": code, "message": message}})


class _ChunkRedactor:
    def __init__(self) -> None:
        self.redactors: dict[int, StreamRedactor] = {}
        self.template: dict | None = None  # envelope of the last chunk, for chunks we synthesize

    def process(self, chunk: dict) -> dict | None:
        """Redact one parsed chunk. Returns None when nothing is left to send."""
        self.template = chunk
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return chunk  # e.g. a trailing usage-only chunk
        kept = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index", 0)
            delta = choice.get("delta")
            redactor = self.redactors.setdefault(index, StreamRedactor())
            text = ""
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text = redactor.feed(delta["content"])
            if choice.get("finish_reason") is not None:
                # This choice is finished: release whatever it was holding.
                text += redactor.flush()
            if not isinstance(delta, dict):
                delta = choice["delta"] = {}
            if text:
                delta["content"] = text
            else:
                delta.pop("content", None)
            if delta or choice.get("finish_reason") is not None:
                kept.append(choice)
        if not kept and not chunk.get("usage"):
            return None
        chunk["choices"] = kept
        return chunk

    def drain(self) -> dict | None:
        """Text still held when the stream ends without a finish_reason."""
        choices = []
        for index, redactor in sorted(self.redactors.items()):
            text = redactor.flush()
            if text:
                choices.append({"index": index, "delta": {"content": text}, "finish_reason": None})
        if not choices:
            return None
        chunk = copy.deepcopy(self.template) if self.template else {"object": "chat.completion.chunk"}
        chunk["choices"] = choices
        chunk.pop("usage", None)
        return chunk


async def redact_sse(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Turn upstream SSE lines into redacted SSE events, one event per data line."""
    state = _ChunkRedactor()
    async for line in lines:
        if not line.startswith("data:"):
            continue  # blank separators, comments, event:/id: fields
        data = line[5:].strip()
        if data == "[DONE]":
            leftover = state.drain()
            if leftover:
                yield sse_event(leftover)
            yield DONE
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            log.warning("dropping undecodable upstream event")
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk:
            # Upstream reported a failure mid-stream. Its message may contain
            # anything, so it is replaced rather than forwarded.
            leftover = state.drain()
            if leftover:
                yield sse_event(leftover)
            yield stream_error_event("upstream_error", "The model provider ended the stream with an error")
            return
        redacted = state.process(chunk)
        if redacted is not None:
            yield sse_event(redacted)

    # Upstream closed without [DONE]: deliver what was held, then end cleanly.
    leftover = state.drain()
    if leftover:
        yield sse_event(leftover)
    yield DONE
