"""Rate limit, then primary model, then secondary on 429 / timeout / outage."""

import asyncio
import logging
import math
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from task4_router_fallback import errors
from task4_router_fallback.limiter import RequestTooLarge, SlidingWindowLimiter
from task4_router_fallback.providers import Provider, ProviderFailure, call_provider, total_tokens
from task4_router_fallback.tokens import estimate_request_tokens

log = logging.getLogger("task4.router")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None  # accepted for client compatibility; the router picks the model
    messages: list[ChatMessage] = Field(min_length=1, max_length=256)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False

    @field_validator("stream")
    @classmethod
    def _no_streaming(cls, value: bool) -> bool:
        if value:
            raise ValueError("streaming is not supported by this endpoint")
        return value

    def upstream_payload(self) -> dict:
        return self.model_dump(exclude={"model", "stream"}, exclude_none=True)


class Router:
    def __init__(
        self,
        limiter: SlidingWindowLimiter,
        client: httpx.AsyncClient,
        primary: Provider,
        secondary: Provider,
    ):
        self.limiter = limiter
        self.client = client
        self.primary = primary
        self.secondary = secondary

    async def complete(self, tenant: str, request: ChatRequest, request_id: str) -> dict:
        estimate = estimate_request_tokens((m.content for m in request.messages), request.max_tokens)

        # SQLite calls block, so they run in a worker thread to keep the event
        # loop free for other requests.
        try:
            decision = await asyncio.to_thread(self.limiter.reserve, tenant, estimate)
        except RequestTooLarge:
            raise errors.request_too_large() from None
        if not decision.allowed:
            log.info("rate limited request_id=%s used=%d estimate=%d", request_id, decision.used, estimate)
            raise errors.rate_limited(max(1, math.ceil(decision.retry_after_s)))

        try:
            body = await self._call_with_fallback(request.upstream_payload(), request_id)
        except BaseException:
            # Nothing was produced, so nothing is charged. Called synchronously:
            # if this task is being cancelled, a further await could be cut short
            # and leave the reservation behind.
            self.limiter.release(decision.reservation_id)
            raise

        actual = total_tokens(body)
        if actual is not None:
            await asyncio.to_thread(self.limiter.reconcile, decision.reservation_id, actual)
        return body

    async def _call_with_fallback(self, payload: dict, request_id: str) -> dict:
        try:
            return await call_provider(self.client, self.primary, payload)
        except ProviderFailure as exc:
            log.warning("primary failed request_id=%s provider=%s reason=%s", request_id, self.primary.name, exc.reason)
            if not exc.failover:
                raise errors.upstream_rejected() from None

        try:
            body = await call_provider(self.client, self.secondary, payload)
        except ProviderFailure as exc:
            log.error("secondary failed request_id=%s provider=%s reason=%s", request_id, self.secondary.name, exc.reason)
            if not exc.failover:
                raise errors.upstream_rejected() from None
            raise errors.upstream_unavailable() from None
        log.info("served by secondary request_id=%s provider=%s", request_id, self.secondary.name)
        return body
