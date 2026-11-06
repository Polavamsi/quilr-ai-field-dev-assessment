"""Calling one OpenAI-compatible chat completions endpoint, with a hard deadline."""

import asyncio
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    model: str
    timeout_s: float
    api_key: str | None = None


class ProviderFailure(Exception):
    """`reason` is for logs. `failover` says whether another provider may succeed."""

    def __init__(self, reason: str, failover: bool):
        super().__init__(reason)
        self.reason = reason
        self.failover = failover


async def call_provider(client: httpx.AsyncClient, provider: Provider, payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
    try:
        # asyncio.timeout bounds the whole exchange: connect, send, wait and
        # read the body. On expiry it cancels the request, which closes the
        # connection, so the abandoned call stops holding a socket.
        async with asyncio.timeout(provider.timeout_s):
            response = await client.post(provider.url, json={**payload, "model": provider.model}, headers=headers)
    except TimeoutError:
        raise ProviderFailure("timeout", failover=True) from None
    except httpx.HTTPError as exc:
        raise ProviderFailure(f"transport error: {type(exc).__name__}", failover=True) from None

    status = response.status_code
    if status == 429:
        raise ProviderFailure("rate limited (429)", failover=True)
    if status >= 500:
        raise ProviderFailure(f"server error ({status})", failover=True)
    if status >= 400:
        # The request itself was refused (bad input, auth). A second provider
        # would refuse the same request, so this does not fail over.
        raise ProviderFailure(f"client error ({status})", failover=False)

    try:
        body = response.json()
    except ValueError:
        raise ProviderFailure("response is not JSON", failover=True) from None
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        raise ProviderFailure("response has no choices", failover=True)
    return body


def total_tokens(body: dict) -> int | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("total_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
