"""
Distributed Rate Limiting Algorithms using Redis Atomic Operations.

Implements two strategies:
  1. Token Bucket  - smooth rate limiting with burst support
  2. Sliding Window - precise per-window request counting
"""

import time
import redis
from typing import Tuple


# ---------------------------------------------------------------------------
# Lua scripts — executed atomically inside Redis (no race conditions)
# ---------------------------------------------------------------------------

TOKEN_BUCKET_SCRIPT = """
local key        = KEYS[1]
local capacity   = tonumber(ARGV[1])   -- max tokens in the bucket
local refill_rate = tonumber(ARGV[2])  -- tokens added per second
local now        = tonumber(ARGV[3])   -- current timestamp (seconds, float)
local requested  = tonumber(ARGV[4])   -- tokens needed for this request (usually 1)

-- Load existing state (tokens, last_refill_time)
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens      = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

-- Refill tokens proportionally to elapsed time
local elapsed = math.max(0, now - last_refill)
local new_tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if new_tokens >= requested then
    new_tokens = new_tokens - requested
    allowed    = 1
end

-- Persist updated state with 2× refill-time TTL so idle keys expire cleanly
local ttl = math.ceil(capacity / refill_rate) * 2
redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', now)
redis.call('EXPIRE', key, ttl)

return {allowed, math.floor(new_tokens), ttl}
"""

SLIDING_WINDOW_SCRIPT = """
local key         = KEYS[1]
local limit       = tonumber(ARGV[1])   -- max requests per window
local window_size = tonumber(ARGV[2])   -- window duration in seconds
local now         = tonumber(ARGV[3])   -- current timestamp (milliseconds)
local request_id  = ARGV[4]            -- unique ID for this request

-- Remove timestamps that have slid out of the window
local window_start = now - (window_size * 1000)
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count remaining requests in the current window
local count = redis.call('ZCARD', key)

local allowed = 0
if count < limit then
    -- Record this request's timestamp
    redis.call('ZADD', key, now, request_id)
    redis.call('EXPIRE', key, window_size + 1)
    allowed = 1
    count   = count + 1
end

return {allowed, count, limit - count}
"""


class TokenBucketLimiter:
    """
    Token Bucket algorithm.

    - Each identifier (user / IP) owns a virtual bucket with `capacity` tokens.
    - Tokens refill at `refill_rate` per second.
    - Each request consumes one token.
    - Allows short bursts up to `capacity` while enforcing a long-term rate.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        capacity: int = 10,
        refill_rate: float = 1.0,
        key_prefix: str = "tb",
    ):
        self.redis = redis_client
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.key_prefix = key_prefix
        self._script = redis_client.register_script(TOKEN_BUCKET_SCRIPT)

    def is_allowed(self, identifier: str, tokens_needed: int = 1) -> Tuple[bool, dict]:
        """
        Check whether `identifier` may proceed.

        Returns:
            (allowed: bool, info: dict)
        """
        key = f"{self.key_prefix}:{identifier}"
        now = time.time()

        result = self._script(
            keys=[key],
            args=[self.capacity, self.refill_rate, now, tokens_needed],
        )

        allowed, remaining_tokens, ttl = result
        info = {
            "algorithm": "token_bucket",
            "allowed": bool(allowed),
            "remaining_tokens": remaining_tokens,
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "retry_after": round((1 - remaining_tokens) / self.refill_rate, 2) if not allowed else 0,
        }
        return bool(allowed), info


class SlidingWindowLimiter:
    """
    Sliding Window Log algorithm.

    - Tracks the timestamp of every request in a Redis sorted set.
    - Counts only requests within the last `window_size` seconds.
    - Provides exact rate limiting (no boundary-spike problem of fixed windows).
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        limit: int = 100,
        window_size: int = 60,
        key_prefix: str = "sw",
    ):
        self.redis = redis_client
        self.limit = limit
        self.window_size = window_size
        self.key_prefix = key_prefix
        self._script = redis_client.register_script(SLIDING_WINDOW_SCRIPT)

    def is_allowed(self, identifier: str) -> Tuple[bool, dict]:
        """
        Check whether `identifier` may proceed.

        Returns:
            (allowed: bool, info: dict)
        """
        key = f"{self.key_prefix}:{identifier}"
        now_ms = int(time.time() * 1000)
        request_id = f"{now_ms}-{id(object())}"  # unique enough without uuid import

        result = self._script(
            keys=[key],
            args=[self.limit, self.window_size, now_ms, request_id],
        )

        allowed, current_count, remaining = result
        info = {
            "algorithm": "sliding_window",
            "allowed": bool(allowed),
            "current_count": current_count,
            "limit": self.limit,
            "remaining": max(0, remaining),
            "window_size": self.window_size,
            "retry_after": self.window_size if not allowed else 0,
        }
        return bool(allowed), info


class HybridRateLimiter:
    """
    Combines Token Bucket (per-user burst control) and
    Sliding Window (per-IP abuse prevention) in a single check.

    A request is allowed only when BOTH limiters approve it.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        user_capacity: int = 20,
        user_refill_rate: float = 2.0,
        ip_limit: int = 100,
        ip_window: int = 60,
    ):
        self.user_limiter = TokenBucketLimiter(
            redis_client,
            capacity=user_capacity,
            refill_rate=user_refill_rate,
            key_prefix="user_tb",
        )
        self.ip_limiter = SlidingWindowLimiter(
            redis_client,
            limit=ip_limit,
            window_size=ip_window,
            key_prefix="ip_sw",
        )

    def is_allowed(self, user_id: str, ip_address: str) -> Tuple[bool, dict]:
        user_allowed, user_info = self.user_limiter.is_allowed(user_id)
        ip_allowed, ip_info = self.ip_limiter.is_allowed(ip_address)

        allowed = user_allowed and ip_allowed
        retry_after = max(user_info.get("retry_after", 0), ip_info.get("retry_after", 0))

        return allowed, {
            "allowed": allowed,
            "retry_after": retry_after,
            "user_limit": user_info,
            "ip_limit": ip_info,
        }
