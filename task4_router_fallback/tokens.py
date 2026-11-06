"""Up-front token estimate used to reserve rate-limit budget before calling a model.

The estimate is deliberately pessimistic: prompt size plus the full completion
budget. After the call the reservation is corrected to the provider's reported
usage, so overestimating only delays requests briefly; underestimating would
let a tenant exceed its limit.
"""

import math
from collections.abc import Iterable

CHARS_PER_TOKEN = 4        # common rule of thumb for English text with BPE tokenizers
PER_MESSAGE_OVERHEAD = 4   # role and separator tokens in chat formats
DEFAULT_MAX_TOKENS = 512   # completion budget when the client does not set max_tokens


def estimate_prompt_tokens(contents: Iterable[str]) -> int:
    return sum(math.ceil(len(text) / CHARS_PER_TOKEN) + PER_MESSAGE_OVERHEAD for text in contents)


def estimate_request_tokens(contents: Iterable[str], max_tokens: int | None) -> int:
    return estimate_prompt_tokens(contents) + (max_tokens or DEFAULT_MAX_TOKENS)
