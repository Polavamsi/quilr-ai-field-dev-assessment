"""PII redaction over text that arrives in arbitrary fragments.

A pattern can straddle two chunks ("john@exa" + "mple.com"), so text cannot
be redacted chunk by chunk. Buffering the whole response is not an option
either: it destroys time to first token and memory grows with output length.

StreamRedactor keeps a small holdback. After each chunk it emits everything
that can no longer become part of a match, and holds only the tail that might
still turn into one:

  * from the start of any partial match at the end of the buffer
    ("contact jo" holds "jo": it may yet become "jo@example.com");
  * from the start of any complete match that could still grow
    ("4111 1111 1111 1111 " may gain digits; "a@b.co." may become "a@b.co.uk").

It never cuts inside a regex match, even one that failed validation, and keeps
the last emitted character as lookbehind context, so the output is identical
however the text is chunked.

Usually that is the last word or digit group. Every pattern has bounded
quantifiers, so a match is never longer than MAX_MATCH_LEN, and the holdback is
capped at that length: anything older cannot belong to a match that is still
open. Memory per stream is O(MAX_MATCH_LEN), not O(response length).
"""

from dataclasses import dataclass

import regex

REDACTED = "[REDACTED]"
MAX_MATCH_LEN = 540  # longest email the pattern allows: 64 + 1 + 63 + 6 * 64 + 1 + 24 = 537; cards and SSNs are shorter


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: regex.Pattern
    validate: object = None  # optional callable(str) -> bool for checks a regex cannot express


def luhn_valid(text: str) -> bool:
    digits = [int(ch) for ch in text if ch.isdigit()]
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


PATTERNS = (
    Pattern(
        "email",
        regex.compile(
            r"(?<![\w.%+-])[A-Za-z0-9._%+-]{1,64}@"
            r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,6}\.[A-Za-z]{2,24}(?![\w-])"
        ),
    ),
    Pattern(
        # Area 000, 666 and 9xx, group 00 and serial 0000 are never issued.
        "ssn",
        regex.compile(r"(?<![\w-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\w-])"),
    ),
    Pattern(
        # 13 to 19 digits, optionally grouped by single spaces or dashes. The
        # Luhn check keeps order numbers and timestamps from being redacted.
        "card",
        regex.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"),
        luhn_valid,
    ),
)


def _scan(text: str, pos: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    """Scan text[pos:], using text[:pos] only as lookbehind context.

    Returns the spans to redact (merged), every complete regex match whether or
    not it passed validation, and the index from which text must be held.
    """
    hold_from = len(text)
    redact: list[tuple[int, int]] = []
    candidates: list[tuple[int, int]] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text, pos=pos, partial=True):
            start, end = match.span()
            if match.partial:
                if end > start:
                    hold_from = min(hold_from, start)
                continue
            # A complete match is only final if no longer match could start at
            # the same place. "x@example.co." completes as "x@example.co" but may
            # become "x@example.co.uk"; "4111 1111 1111 1111 " may gain digits.
            # Test whether everything from the match start to the end of the
            # buffer could still be the beginning of a single match.
            if pattern.regex.fullmatch(text, pos=start, partial=True):
                hold_from = min(hold_from, start)
                continue
            candidates.append((start, end))
            if pattern.validate is None or pattern.validate(match.group()):
                redact.append((start, end))
    redact.sort()
    merged: list[tuple[int, int]] = []
    for start, end in redact:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged, candidates, hold_from


def _redact(text: str, spans: list[tuple[int, int]], start: int, stop: int) -> str:
    """text[start:stop] with every span replaced by REDACTED."""
    out, last = [], start
    for span_start, span_end in spans:
        out.append(text[last:span_start])
        out.append(REDACTED)
        last = span_end
    out.append(text[last:stop])
    return "".join(out)


def redact_text(text: str) -> str:
    """Redact a complete string (non-streaming responses)."""
    redactor = StreamRedactor()
    return redactor.feed(text) + redactor.flush()


class StreamRedactor:
    def __init__(self) -> None:
        self._pending = ""
        # The last character already emitted. Every pattern has a one-character
        # lookbehind, so this is all the left context a later scan needs.
        self._context = ""

    @property
    def pending_len(self) -> int:
        return len(self._pending)

    def feed(self, chunk: str) -> str:
        """Add a fragment; return the redacted text that is now safe to emit."""
        if not chunk:
            return ""
        base = len(self._context)
        text = self._context + self._pending + chunk
        redact, candidates, hold_from = _scan(text, base)

        # Text older than MAX_MATCH_LEN from the end cannot be inside an
        # unfinished match, whatever the patterns report.
        cut = max(hold_from, len(text) - MAX_MATCH_LEN)
        # Never cut inside a regex match, even one that failed validation:
        # "99 5500000000000004" is not a valid card, but cut after "99 " and
        # the rest would be rescanned as one.
        moved = True
        while moved:
            moved = False
            for start, end in candidates:
                if start < cut < end:
                    cut, moved = start, True

        emitted = _redact(text, [(s, e) for s, e in redact if e <= cut], base, cut)
        if cut > base:
            self._context = text[cut - 1]
        self._pending = text[cut:]
        return emitted

    def flush(self) -> str:
        """End of stream: everything held is final."""
        if not self._pending:
            return ""
        base = len(self._context)
        # A trailing newline lets matches at the very end complete.
        text = self._context + self._pending + "\n"
        redact, _, _ = _scan(text, base)
        end = len(text) - 1
        emitted = _redact(text, [(s, min(e, end)) for s, e in redact], base, end)
        self._pending, self._context = "", ""
        return emitted
