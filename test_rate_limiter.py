"""
Test suite for the Distributed Rate Limiter.
Requires a local Redis instance on DB 15 (isolated from dev/prod data).

Run:
    APP_ENV=testing pytest test_rate_limiter.py -v
"""

import time
import pytest
import redis

from rate_limiter.algorithms import (
    TokenBucketLimiter,
    SlidingWindowLimiter,
    HybridRateLimiter,
)
from rate_limiter.config import TestingConfig

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def redis_client():
    """Connect to the isolated test Redis DB and flush it before the session."""
    cfg = TestingConfig()
    client = redis.Redis(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        db=cfg.REDIS_DB,       # DB 15 — test only
        decode_responses=True,
    )
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture(autouse=True)
def flush_between_tests(redis_client):
    """Clear all keys before each test for a clean slate."""
    redis_client.flushdb()
    yield
    redis_client.flushdb()


# ── Token Bucket Tests ────────────────────────────────────────────────────────

class TestTokenBucket:
    @pytest.fixture
    def limiter(self, redis_client):
        return TokenBucketLimiter(
            redis_client,
            capacity=5,
            refill_rate=1.0,   # 1 token/sec
            key_prefix="test_tb",
        )

    def test_allows_requests_within_capacity(self, limiter):
        for _ in range(5):
            allowed, info = limiter.is_allowed("user1")
            assert allowed, f"Expected allowed but got: {info}"

    def test_blocks_after_capacity_exceeded(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user1")
        allowed, info = limiter.is_allowed("user1")
        assert not allowed
        assert info["remaining_tokens"] == 0

    def test_independent_buckets_per_user(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user_a")
        # user_b should still have a full bucket
        allowed, _ = limiter.is_allowed("user_b")
        assert allowed

    def test_tokens_refill_over_time(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user1")

        # Wait for 2 tokens to refill
        time.sleep(2.1)
        allowed, info = limiter.is_allowed("user1")
        assert allowed
        assert info["remaining_tokens"] >= 1

    def test_info_structure(self, limiter):
        _, info = limiter.is_allowed("user1")
        assert "algorithm" in info
        assert "allowed" in info
        assert "remaining_tokens" in info
        assert "capacity" in info
        assert "refill_rate" in info

    def test_retry_after_when_blocked(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user1")
        allowed, info = limiter.is_allowed("user1")
        assert not allowed
        assert info["retry_after"] > 0


# ── Sliding Window Tests ──────────────────────────────────────────────────────

class TestSlidingWindow:
    @pytest.fixture
    def limiter(self, redis_client):
        return SlidingWindowLimiter(
            redis_client,
            limit=5,
            window_size=5,    # 5-second window
            key_prefix="test_sw",
        )

    def test_allows_requests_within_limit(self, limiter):
        for _ in range(5):
            allowed, info = limiter.is_allowed("ip1")
            assert allowed

    def test_blocks_when_limit_exceeded(self, limiter):
        for _ in range(5):
            limiter.is_allowed("ip1")
        allowed, info = limiter.is_allowed("ip1")
        assert not allowed
        assert info["remaining"] == 0

    def test_independent_windows_per_ip(self, limiter):
        for _ in range(5):
            limiter.is_allowed("ip_a")
        allowed, _ = limiter.is_allowed("ip_b")
        assert allowed

    def test_old_requests_slide_out(self, limiter):
        for _ in range(5):
            limiter.is_allowed("ip1")

        # Wait for the full window to expire
        time.sleep(5.2)
        allowed, _ = limiter.is_allowed("ip1")
        assert allowed

    def test_info_structure(self, limiter):
        _, info = limiter.is_allowed("ip1")
        assert "algorithm" in info
        assert "current_count" in info
        assert "limit" in info
        assert "remaining" in info
        assert "window_size" in info

    def test_remaining_decrements(self, limiter):
        _, info1 = limiter.is_allowed("ip1")
        _, info2 = limiter.is_allowed("ip1")
        assert info2["remaining"] < info1["remaining"]


# ── Hybrid Limiter Tests ──────────────────────────────────────────────────────

class TestHybridRateLimiter:
    @pytest.fixture
    def limiter(self, redis_client):
        return HybridRateLimiter(
            redis_client,
            user_capacity=5,
            user_refill_rate=1.0,
            ip_limit=10,
            ip_window=10,
        )

    def test_allows_normal_requests(self, limiter):
        allowed, info = limiter.is_allowed("user1", "1.2.3.4")
        assert allowed
        assert info["allowed"]

    def test_blocks_when_user_bucket_empty(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user1", "1.2.3.4")
        allowed, info = limiter.is_allowed("user1", "1.2.3.4")
        assert not allowed
        assert not info["user_limit"]["allowed"]

    def test_blocks_when_ip_window_full(self, limiter):
        for _ in range(10):
            # Different users to avoid user bucket exhaustion
            limiter.is_allowed(f"user_{_}", "shared_ip")
        # Any user from this IP should now be blocked
        allowed, info = limiter.is_allowed("new_user", "shared_ip")
        assert not allowed
        assert not info["ip_limit"]["allowed"]

    def test_info_has_both_limits(self, limiter):
        _, info = limiter.is_allowed("user1", "1.2.3.4")
        assert "user_limit" in info
        assert "ip_limit" in info
        assert "retry_after" in info


# ── Concurrency smoke test ────────────────────────────────────────────────────

class TestConcurrency:
    def test_atomic_operations_prevent_over_admission(self, redis_client):
        """
        Fire many simultaneous requests and verify the total admitted never
        exceeds the configured limit — validating Lua atomicity.
        """
        import threading

        limiter = SlidingWindowLimiter(
            redis_client,
            limit=10,
            window_size=60,
            key_prefix="concurrent_test",
        )

        results = []
        lock = threading.Lock()

        def make_request():
            allowed, _ = limiter.is_allowed("concurrent_user")
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=make_request) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        admitted = sum(results)
        assert admitted <= 10, f"Admitted {admitted} but limit is 10 — atomicity failed!"
        assert admitted == 10, f"Expected exactly 10 admitted, got {admitted}"
