import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from task4_router_fallback.limiter import RequestTooLarge, SlidingWindowLimiter


class FakeClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(tmp_path, clock):
    return SlidingWindowLimiter(str(tmp_path / "limits.db"), limit=50_000, window_s=60, clock=clock)


def row_count(limiter) -> int:
    with sqlite3.connect(limiter.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]


def test_admits_up_to_the_limit_then_rejects(limiter):
    assert limiter.reserve("t1", 30_000).allowed
    assert limiter.reserve("t1", 20_000).allowed       # exactly 50,000
    denied = limiter.reserve("t1", 1)
    assert not denied.allowed
    assert denied.used == 50_000
    assert limiter.usage("t1") == 50_000                # a denial records nothing


def test_window_slides_rather_than_resetting(limiter, clock):
    limiter.reserve("t1", 30_000)                       # t=0
    clock.now += 30
    limiter.reserve("t1", 20_000)                       # t=30
    clock.now += 29                                     # t=59: both still inside the window
    denied = limiter.reserve("t1", 1_000)
    assert not denied.allowed
    assert denied.retry_after_s == pytest.approx(1.0)  # the 30k row expires at t=60

    clock.now += 1                                      # t=60: first row has expired
    assert limiter.reserve("t1", 1_000).allowed
    assert limiter.usage("t1") == 21_000


def test_retry_after_waits_for_enough_tokens_to_expire(limiter, clock):
    for _ in range(5):
        limiter.reserve("t1", 10_000)                   # rows at t=0,10,20,30,40
        clock.now += 10
    # t=50, used=50,000. A 25k request needs 25k freed: rows at 0, 10 and 20.
    denied = limiter.reserve("t1", 25_000)
    assert denied.retry_after_s == pytest.approx(30.0)  # the t=20 row expires at t=80


def test_expired_rows_are_deleted(limiter, clock):
    limiter.reserve("t1", 100)
    limiter.reserve("t2", 100)
    clock.now += 61
    limiter.reserve("t3", 100)
    assert row_count(limiter) == 1


def test_tenants_are_isolated(limiter):
    limiter.reserve("t1", 50_000)
    assert not limiter.reserve("t1", 1).allowed
    assert limiter.reserve("t2", 50_000).allowed


def test_reconcile_replaces_estimate_with_actual(limiter):
    decision = limiter.reserve("t1", 40_000)
    limiter.reconcile(decision.reservation_id, 1_200)
    assert limiter.usage("t1") == 1_200
    assert limiter.reserve("t1", 48_000).allowed


def test_release_frees_the_reservation(limiter):
    decision = limiter.reserve("t1", 40_000)
    limiter.release(decision.reservation_id)
    assert limiter.usage("t1") == 0


def test_single_request_over_the_limit_is_refused(limiter):
    with pytest.raises(RequestTooLarge):
        limiter.reserve("t1", 50_001)


def test_state_survives_restart(tmp_path, clock):
    path = str(tmp_path / "limits.db")
    SlidingWindowLimiter(path, limit=50_000, clock=clock).reserve("t1", 45_000)
    reopened = SlidingWindowLimiter(path, limit=50_000, clock=clock)
    assert reopened.usage("t1") == 45_000
    assert not reopened.reserve("t1", 10_000).allowed


def test_concurrent_reservations_never_exceed_the_limit(tmp_path):
    # 40 threads race to reserve 5,000 tokens each against a 50,000 limit.
    # Exactly 10 may win. Without BEGIN IMMEDIATE, several threads read the
    # same total and over-admit.
    limiter = SlidingWindowLimiter(str(tmp_path / "limits.db"), limit=50_000)
    with ThreadPoolExecutor(max_workers=40) as pool:
        results = list(pool.map(lambda _: limiter.reserve("t1", 5_000).allowed, range(40)))
    assert results.count(True) == 10
    assert limiter.usage("t1") == 50_000


def test_concurrent_reservations_across_separate_limiter_instances(tmp_path):
    # Two gateway processes sharing one database file, simulated with two instances.
    path = str(tmp_path / "limits.db")
    a = SlidingWindowLimiter(path, limit=50_000)
    b = SlidingWindowLimiter(path, limit=50_000)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: (a if i % 2 else b).reserve("t1", 5_000).allowed, range(20)))
    assert results.count(True) == 10
