import random

import pytest

from task3_pii_stream_guardrail.redactor import MAX_MATCH_LEN, REDACTED, StreamRedactor, luhn_valid, redact_text

SAMPLE = (
    "Customer Jane can be reached at jane.doe+billing@example.co.uk or j.doe@corp.example.com. "
    "Her SSN is 123-45-6789 and her card is 4111 1111 1111 1111, backup card 5500-0000-0000-0004. "
    "Order 1234567890123 is not a card. Version 2.4.1 shipped on 2026-11-05. Contact support@quilr.ai today."
)
SAMPLE_EXPECTED = (
    f"Customer Jane can be reached at {REDACTED} or {REDACTED}. "
    f"Her SSN is {REDACTED} and her card is {REDACTED}, backup card {REDACTED}. "
    f"Order 1234567890123 is not a card. Version 2.4.1 shipped on 2026-11-05. Contact {REDACTED} today."
)


def stream(chunks: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(c) for c in chunks) + redactor.flush()


def split_at(text: str, cuts: list[int]) -> list[str]:
    bounds = [0, *sorted(cuts), len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:])]


# ---------- what gets redacted ----------

@pytest.mark.parametrize(
    "text",
    [
        "a@b.co",
        "first.last+tag@sub.example.com",
        "USER_99@example-mail.io",
        "123-45-6789",
        "123 45 6789",
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "378282246310005",      # Amex, 15 digits
        "6011111111111117",     # Discover
        "4222222222222",        # 13-digit Visa
    ],
)
def test_pii_is_redacted(text):
    assert redact_text(f"see {text} here") == f"see {REDACTED} here"


@pytest.mark.parametrize(
    "text",
    [
        "4111111111111112",     # fails Luhn
        "1234567890123",        # order number, fails Luhn
        "000-12-3456",          # never-issued area
        "666-12-3456",
        "900-12-3456",
        "123-00-6789",
        "123-45-0000",
        "123-45 6789",          # mixed separators
        "2026-11-05",
        "v2.4.1",
        "not an @ email",
        "user@localhost",       # no TLD
        "12345",
    ],
)
def test_non_pii_is_left_alone(text):
    assert redact_text(f"see {text} here") == f"see {text} here"


def test_luhn():
    assert luhn_valid("4111 1111 1111 1111")
    assert not luhn_valid("4111 1111 1111 1112")


def test_sample_redacts_as_expected():
    assert redact_text(SAMPLE) == SAMPLE_EXPECTED


# ---------- chunk boundaries ----------

def test_every_single_split_point_gives_the_same_output():
    for cut in range(1, len(SAMPLE)):
        assert stream(split_at(SAMPLE, [cut])) == SAMPLE_EXPECTED, f"split at {cut}"


def test_character_by_character():
    assert stream(list(SAMPLE)) == SAMPLE_EXPECTED


def test_random_multi_way_splits():
    rng = random.Random(1234)
    for _ in range(2_000):
        cuts = rng.sample(range(1, len(SAMPLE)), rng.randint(2, 40))
        assert stream(split_at(SAMPLE, cuts)) == SAMPLE_EXPECTED


@pytest.mark.parametrize(
    "chunks",
    [
        ["Mail john@exa", "mple.com now"],
        ["Mail john", "@", "example", ".", "com now"],
        ["SSN 123-", "45-", "6789 ok"],
        ["Card 4111 1111 ", "1111 1111 ok"],
        ["Card 41111111", "11111111 ok"],
    ],
)
def test_pii_split_across_chunks(chunks):
    out = stream(chunks)
    assert out.endswith(" ok") or out.endswith(" now")
    assert REDACTED in out
    for fragment in ("john", "example", "6789", "4111"):
        assert fragment not in out


def test_partial_pii_is_never_emitted_early():
    redactor = StreamRedactor()
    emitted = redactor.feed("Write to john.smith")
    assert "john" not in emitted        # might still become an email
    emitted += redactor.feed("@example.com please")
    emitted += redactor.flush()
    assert emitted == f"Write to {REDACTED} please"


def test_match_at_end_waits_for_more_text():
    # "a@example.co" is already a full email, but the next chunk could extend it.
    redactor = StreamRedactor()
    first = redactor.feed("hi a@example.co")
    assert first == "hi "
    assert first + redactor.feed("m.au bye") + redactor.flush() == f"hi {REDACTED} bye"


def test_domain_dot_at_chunk_end_does_not_leak_the_tld():
    # Regression: "...example.co." looked like a finished email followed by a period.
    assert stream(["mail jane@example.co.", "uk now"]) == f"mail {REDACTED} now"
    assert stream(["mail jane@example.co", ".", "uk now"]) == f"mail {REDACTED} now"
    assert stream(["mail jane@example.com.", " Bye"]) == f"mail {REDACTED}. Bye"


def test_flush_redacts_pii_at_the_very_end():
    assert stream(["call ", "me at a@b.com"]) == f"call me at {REDACTED}"
    assert stream(["card 4111 1111 1111 1111"]) == f"card {REDACTED}"


def test_plain_text_flows_with_only_the_last_word_held():
    redactor = StreamRedactor()
    assert redactor.feed("The quick brown fox jumps") == "The quick brown fox "
    assert redactor.pending_len == len("jumps")


# ---------- memory ----------

def test_holdback_stays_bounded_on_long_streams():
    rng = random.Random(7)
    words = ["lorem", "ipsum", "4111", "1111", "a@b", "123-45", "x" * 50, "-", "  "]
    redactor = StreamRedactor()
    longest = 0
    for _ in range(20_000):
        redactor.feed(rng.choice(words) + rng.choice([" ", "", "."]))
        longest = max(longest, redactor.pending_len)
    assert longest <= MAX_MATCH_LEN + 60  # bounded by the cap plus one chunk


def test_unbroken_run_without_spaces_is_not_buffered_forever():
    redactor = StreamRedactor()
    out = "".join(redactor.feed("a" * 100) for _ in range(50))
    assert redactor.pending_len <= MAX_MATCH_LEN
    assert len(out) >= 5_000 - MAX_MATCH_LEN


def test_invalid_candidate_is_not_split_into_a_valid_one():
    # Regression: "99 5500000000000004" is 18 digits failing Luhn, so it is not
    # a card. A cut after "99 " once left "5500000000000004", a valid card.
    text = "id 99 5500000000000004.a@b.co done"
    expected = redact_text(text)
    for cut in range(1, len(text)):
        assert stream(split_at(text, [cut])) == expected, f"split at {cut}"


def test_fuzz_streaming_matches_one_shot():
    fragments = [
        "a@b.co", "x.y+z@mail.example.org", "user@host", "123-45-6789", "123 45 6789", "4111 1111 1111 1111",
        "4111-1111-1111-1111", "5500000000000004", "1234567890123", "2026-11-05", "v2.4.1", "hello", "world",
        "@", ".", "-", " ", ", ", ".uk", "@x", "99", "0000", "co", "\n", "jane", "e.com", "4", "1",
    ]
    rng = random.Random(2026)
    for _ in range(3_000):
        text = "".join(rng.choice(fragments) + rng.choice(["", " ", " ", ".", ","]) for _ in range(rng.randint(2, 14)))
        if len(text) < 2:
            continue
        cuts = rng.sample(range(1, len(text)), min(len(text) - 1, rng.randint(1, 10)))
        assert stream(split_at(text, cuts)) == redact_text(text), repr(text)
