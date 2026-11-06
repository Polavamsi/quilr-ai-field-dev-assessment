# Task 3: Streaming PII redaction guardrail

An OpenAI-compatible `POST /v1/chat/completions` proxy. It forwards the request to the upstream model and streams the reply back with emails, US SSNs and payment card numbers replaced by `[REDACTED]`, as the tokens arrive.

## Run

```bash
# mock model: streams a reply full of PII in 1-6 character pieces, 20 ms apart
uv run uvicorn task3_pii_stream_guardrail.mock_llm:app --port 9201

# guardrail
uv run uvicorn task3_pii_stream_guardrail.app:create_app_from_env --factory --port 8100

# client: -N disables curl's buffering so you can watch the stream
curl -N localhost:8100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"m","stream":true,"messages":[{"role":"user","content":"account details?"}]}'
```

`UPSTREAM_URL` and `UPSTREAM_API_KEY` point it at a real provider instead, e.g. `UPSTREAM_URL=https://api.openai.com/v1/chat/completions`.

## The problem

Tokens arrive in fragments, and PII straddles them: `"jane.doe@exa"` + `"mple.com"`, `"4111 1111 "` + `"1111 1111"`. Running a regex on each chunk misses these. Buffering the whole reply catches them but destroys time to first token, and memory grows with the response.

## The approach: a minimal holdback

`StreamRedactor` (`redactor.py`) keeps a pending tail per stream. On each chunk it scans `pending + chunk` and emits everything that can no longer become part of a match. It holds back from the earliest of:

- a **partial match** at the end of the buffer (`"contact jo"` holds `"jo"`, which could become `jo@example.com`), found with the `regex` module's `partial=True` matching;
- a **complete match that could still grow** (`"a@example.co."` matches as `a@example.co` but may become `a@example.co.uk`; `"4111 1111 1111 1111 "` may gain digits), detected by asking whether the text from the match start to the end of the buffer is still the prefix of a single match.

So for ordinary prose only the last word is held. Two rules keep the output identical however the text is chunked:

- A cut never lands inside any regex match, including ones that fail validation. `99 5500000000000004` fails the Luhn check as a whole; cut after `99 ` and the remainder would be rescanned as a valid card.
- The last emitted character is kept as left context, because every pattern has a one-character lookbehind.

**Memory is bounded.** Every pattern uses bounded quantifiers, so no match exceeds 537 characters. Anything older than that cannot be inside an open match and is released, whatever the scan says. Each stream holds at most `MAX_MATCH_LEN` (540) characters plus one chunk, no matter how long the reply is. Each scan covers only that tail, so work per chunk is constant and total work is linear in the response length.

At the end of the stream, or when a choice reports `finish_reason`, the tail is flushed and redacted. Each choice index gets its own redactor, so `n > 1` responses do not mix.

## What is detected

| Type | Rule |
| --- | --- |
| Email | `local@domain.tld`, local part ≤ 64 chars, TLD of 2–24 letters |
| SSN | `AAA-GG-SSSS` or with spaces, same separator twice; excludes never-issued area 000, 666, 9xx, group 00 and serial 0000 |
| Card | 13–19 digits, optionally grouped by single spaces or dashes, **Luhn-valid** (so order numbers and IDs survive) |

## Measured cost

`uv run python -m task3_pii_stream_guardrail.bench` streams the same reply directly from the mock and through the guardrail, over real HTTP, 20 runs each (MacBook Air, Python 3.12):

|  | TTFT median | TTFT p95 | Total median |
| --- | --- | --- | --- |
| Direct to model | 36.1 ms | 36.8 ms | 1432.5 ms |
| Through guardrail | 60.6 ms | 67.2 ms | 1436.9 ms |

Total stream time rises by 4 ms (0.3%). TTFT rises by about one upstream chunk interval (20 ms here). That cost is inherent: the reply's first word is held until the next chunk shows it is not the start of an email. Any correct streaming redactor has to wait that long.

## Wire format

Output is the same SSE format as the upstream: same `id`, `model` and `object`, one event per upstream event, `data: [DONE]` at the end. Events whose content was entirely held back are skipped, except for role and `finish_reason` events. `Cache-Control: no-cache` and `X-Accel-Buffering: no` keep intermediate proxies from buffering the stream. Non-streaming requests get the full message redacted.

Upstream failures are never relayed: HTTP errors become `{"error": {"code": "upstream_error", ...}}` (429 stays 429, everything else 502), and an error event inside the stream is replaced by a fixed message.

## Tests

```bash
uv run pytest tests/test_task3_redactor.py tests/test_task3_stream.py -v
```

- Every single split point of a PII-heavy sample, character-by-character streaming, and 2,000 random multi-way splits must equal the one-shot redaction.
- A fuzzer builds 3,000 random texts from PII and look-alike fragments, splits each at random, and compares streamed against one-shot output. During development, 90,000 fuzz cases found the two chunking bugs described above; both have regression tests.
- Holdback stays bounded over a 20,000-chunk stream.
- The first redacted text leaves the guardrail while the upstream is still under a tenth of the way through.
- Proxy tests cover the SSE envelope, `n > 1` choices, streams that end without `[DONE]`, sanitized upstream errors, and non-streaming responses.

## Limitations

- Regex detection covers structured PII only. Names, addresses and free-form identifiers need an NER model, which trades a much larger latency budget for recall.
- `tool_calls[].function.arguments` deltas are passed through unredacted. They are JSON fragments and would need the same holdback applied per tool call.
- Phone numbers, IBANs and passport numbers are not covered; adding a pattern means adding one bounded regex to `PATTERNS`.
