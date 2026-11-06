import asyncio
import json

import httpx
import pytest

from task3_pii_stream_guardrail.app import Settings, create_app
from task3_pii_stream_guardrail.mock_llm import DEMO_REPLY, create_mock_llm, split_reply
from task3_pii_stream_guardrail.redactor import REDACTED, redact_text
from task3_pii_stream_guardrail.sse import redact_sse

PII = ["jane.doe", "example.co.uk", "123-45-6789", "4111", "5500", "support@example.com"]


def chunk_line(delta: dict, finish_reason=None, index=0) -> str:
    payload = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": index, "delta": delta, "finish_reason": finish_reason}]}
    return "data: " + json.dumps(payload)


def upstream_lines(pieces: list[str], done=True, finish=True) -> list[str]:
    lines = [chunk_line({"role": "assistant", "content": ""}), ""]
    for piece in pieces:
        lines += [chunk_line({"content": piece}), ""]
    if finish:
        lines += [chunk_line({}, finish_reason="stop"), ""]
    if done:
        lines += ["data: [DONE]", ""]
    return lines


async def aiter(items, delay=0.0):
    for item in items:
        if delay:
            await asyncio.sleep(delay)
        yield item


async def collect(lines) -> tuple[str, list[dict], list[str]]:
    events = [e async for e in redact_sse(aiter(lines))]
    payloads = [json.loads(e[6:]) for e in events if e.startswith("data: {")]
    text = "".join(
        c["delta"].get("content", "") for p in payloads for c in p.get("choices", []) if "delta" in c
    )
    return text, payloads, events


# ---------- SSE layer ----------

async def test_stream_text_equals_one_shot_redaction():
    text, payloads, events = await collect(upstream_lines(split_reply(DEMO_REPLY, seed=3)))
    assert text == redact_text(DEMO_REPLY)
    assert text.count(REDACTED) == 5
    for fragment in PII:
        assert fragment not in text
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.parametrize("seed", range(25))
async def test_many_chunkings(seed):
    text, _, _ = await collect(upstream_lines(split_reply(DEMO_REPLY, seed=seed)))
    assert text == redact_text(DEMO_REPLY)


async def test_envelope_preserved_and_role_chunk_forwarded_first():
    _, payloads, _ = await collect(upstream_lines(["Hello ", "world"]))
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert all(p["id"] == "c1" and p["object"] == "chat.completion.chunk" for p in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


async def test_held_text_is_released_with_finish_reason():
    _, payloads, _ = await collect(upstream_lines(["mail me at a@b.com"]))
    last = payloads[-1]["choices"][0]
    assert last["finish_reason"] == "stop"
    assert last["delta"]["content"] == REDACTED


async def test_stream_without_done_or_finish_still_delivers_held_text():
    text, _, events = await collect(upstream_lines(["card 4111 1111 1111 1111"], done=False, finish=False))
    assert text == f"card {REDACTED}"
    assert events[-1] == "data: [DONE]\n\n"


async def test_choices_redacted_independently():
    lines = [
        chunk_line({"content": "a: jane@exa"}, index=0), chunk_line({"content": "b: 123-45-"}, index=1),
        chunk_line({"content": "mple.com"}, index=0), chunk_line({"content": "6789"}, index=1),
        chunk_line({}, "stop", index=0), chunk_line({}, "stop", index=1), "data: [DONE]",
    ]
    _, payloads, _ = await collect(lines)
    by_index = {0: "", 1: ""}
    for p in payloads:
        for c in p["choices"]:
            by_index[c["index"]] += c["delta"].get("content", "")
    assert by_index == {0: f"a: {REDACTED}", 1: f"b: {REDACTED}"}


async def test_upstream_error_event_is_replaced():
    lines = [chunk_line({"content": "partial "}), 'data: {"error": {"message": "OOM at /srv/gpu-7 key=sk-live-123"}}']
    _, _, events = await collect(lines)
    joined = "".join(events)
    assert "sk-live-123" not in joined and "/srv/gpu-7" not in joined
    assert json.loads(events[-1][6:])["error"]["code"] == "upstream_error"


async def test_junk_lines_are_ignored():
    lines = [": keep-alive", "event: message", "data: {not json", *upstream_lines(["ok"])]
    text, _, _ = await collect(lines)
    assert text == "ok"


async def test_first_output_arrives_before_upstream_finishes():
    # 200 pieces 5 ms apart: upstream takes about a second. The first redacted
    # text must come out long before that; the guardrail must not wait for the end.
    pieces = split_reply(DEMO_REPLY * 3, seed=1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    first_text_at = None
    async for event in redact_sse(aiter(upstream_lines(pieces), delay=0.005)):
        if first_text_at is None and '"content":"' in event and '"content":""' not in event:
            first_text_at = loop.time() - start
    total = loop.time() - start
    assert first_text_at is not None
    assert first_text_at < total / 10


# ---------- HTTP proxy ----------

@pytest.fixture
def upstream_app():
    return create_mock_llm(chunk_delay_s=0)


@pytest.fixture
async def proxy(upstream_app):
    to_upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app), base_url="http://llm")
    app = create_app(Settings(upstream_url="http://llm/v1/chat/completions"), client=to_upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        yield client
    await to_upstream.aclose()


REQUEST = {"model": "m", "messages": [{"role": "user", "content": "account details?"}]}


async def test_proxy_streams_redacted_sse(proxy):
    resp = await proxy.post("/v1/chat/completions", json={**REQUEST, "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    text = ""
    for line in resp.text.splitlines():
        if line.startswith("data: {"):
            for c in json.loads(line[6:])["choices"]:
                text += c["delta"].get("content", "")
    assert text == redact_text(DEMO_REPLY)
    assert resp.text.rstrip().endswith("data: [DONE]")


async def test_proxy_redacts_non_streaming_response(proxy):
    resp = await proxy.post("/v1/chat/completions", json=REQUEST)
    assert resp.json()["choices"][0]["message"]["content"] == redact_text(DEMO_REPLY)


@pytest.mark.parametrize("body", [b"{nope", b"[]", b'{"model": "m"}'])
async def test_proxy_rejects_bad_requests(proxy, body):
    resp = await proxy.post("/v1/chat/completions", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


@pytest.mark.parametrize("status,expected", [(429, 429), (500, 502), (401, 502)])
async def test_upstream_http_errors_are_sanitized(status, expected):
    def handler(request):
        return httpx.Response(status, text="Traceback: key sk-live-999 at /srv/llm/worker.py")

    app = create_app(Settings(upstream_url="http://llm/v1"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        resp = await client.post("/v1/chat/completions", json={**REQUEST, "stream": True})
    assert resp.status_code == expected
    assert "sk-live-999" not in resp.text and "worker.py" not in resp.text
    assert resp.json()["error"]["code"] == "upstream_error"


async def test_upstream_down_is_502():
    def handler(request):
        raise httpx.ConnectError("refused 10.1.2.3")

    app = create_app(Settings(upstream_url="http://llm/v1"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        resp = await client.post("/v1/chat/completions", json=REQUEST)
    assert resp.status_code == 502
    assert "10.1.2.3" not in resp.text
