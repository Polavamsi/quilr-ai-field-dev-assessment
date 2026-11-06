"""Token-aware sliding-window rate limiter backed by an on-disk SQLite file.

Each admitted request is one row (tenant, timestamp, tokens). A tenant's usage
is the sum of its rows from the last `window_s` seconds. This is an exact
sliding log rather than fixed buckets, so there is no burst at bucket edges.
"""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id     INTEGER PRIMARY KEY,
    tenant TEXT    NOT NULL,
    ts     REAL    NOT NULL,
    tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS token_usage_tenant_ts ON token_usage (tenant, ts);
CREATE INDEX IF NOT EXISTS token_usage_ts ON token_usage (ts);
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reservation_id: int | None = None
    used: int = 0
    retry_after_s: float = 0.0


class RequestTooLarge(Exception):
    """The request alone needs more tokens than the whole window allows."""


class SlidingWindowLimiter:
    def __init__(
        self,
        db_path: str,
        limit: int = 50_000,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        self.db_path = db_path
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        with closing(self._connect()) as conn:
            # WAL lets readers proceed while a writer holds the lock.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per call: sqlite3 connections must not be
        # shared across threads, and calls arrive from a thread pool.
        # isolation_level=None means transactions are only the explicit ones below.
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def reserve(self, tenant: str, tokens: int) -> Decision:
        """Admit and record `tokens` for `tenant` if the window has room."""
        if tokens > self.limit:
            raise RequestTooLarge
        now = self.clock()
        with closing(self._connect()) as conn:
            # Check-then-insert must be atomic. Without a transaction, two
            # requests can both read usage=45k, both see room for 5k more, and
            # both insert: 55k admitted against a 50k limit. BEGIN IMMEDIATE
            # takes the write lock up front. A plain BEGIN only works here
            # because the DELETE happens to write first; reorder the statements
            # and the lock upgrade can fail with "database is locked" instead
            # of waiting.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM token_usage WHERE ts <= ?", (now - self.window_s,))
                (used,) = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ?", (tenant,)
                ).fetchone()
                if used + tokens > self.limit:
                    retry_after = self._retry_after(conn, tenant, used + tokens - self.limit, now)
                    conn.execute("COMMIT")  # keep the eviction
                    return Decision(allowed=False, used=used, retry_after_s=retry_after)
                cursor = conn.execute(
                    "INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)", (tenant, now, tokens)
                )
                conn.execute("COMMIT")
                return Decision(allowed=True, reservation_id=cursor.lastrowid, used=used + tokens)
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _retry_after(self, conn: sqlite3.Connection, tenant: str, excess: int, now: float) -> float:
        # Walk the tenant's rows oldest first until enough tokens would have
        # expired to fit this request. That row's expiry is the wait.
        freed = 0
        for ts, tokens in conn.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? ORDER BY ts", (tenant,)
        ):
            freed += tokens
            if freed >= excess:
                return max(0.0, ts + self.window_s - now)
        return self.window_s

    def reconcile(self, reservation_id: int, actual_tokens: int) -> None:
        """Replace the up-front estimate with the provider's reported usage."""
        with closing(self._connect()) as conn:
            conn.execute("UPDATE token_usage SET tokens = ? WHERE id = ?", (actual_tokens, reservation_id))

    def release(self, reservation_id: int) -> None:
        """Drop a reservation for a request that consumed nothing."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM token_usage WHERE id = ?", (reservation_id,))

    def usage(self, tenant: str) -> int:
        cutoff = self.clock() - self.window_s
        with closing(self._connect()) as conn:
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?", (tenant, cutoff)
            ).fetchone()
        return used
