"""
Flask middleware / decorator that enforces rate limits on every request.

Usage
-----
Apply @rate_limit() on individual routes, or call init_rate_limiter(app)
to protect ALL routes automatically.
"""

import functools
import logging
from typing import Callable

import redis
from flask import Flask, request, jsonify, g

from .algorithms import HybridRateLimiter, TokenBucketLimiter, SlidingWindowLimiter
from .config import get_config

logger = logging.getLogger(__name__)


def _get_redis_client(cfg) -> redis.Redis:
    """Create and return a Redis client with connection pooling."""
    pool = redis.ConnectionPool(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        db=cfg.REDIS_DB,
        password=cfg.REDIS_PASSWORD,
        socket_timeout=cfg.REDIS_SOCKET_TIMEOUT,
        decode_responses=True,
        max_connections=20,
    )
    return redis.Redis(connection_pool=pool)


def _get_client_ip() -> str:
    """Extract real client IP, respecting common proxy headers."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _get_user_id() -> str:
    """
    Extract user identifier from the request.
    Priority: X-User-ID header → API key → IP fallback.
    """
    return (
        request.headers.get("X-User-ID")
        or request.headers.get("X-API-Key")
        or f"anon:{_get_client_ip()}"
    )


def _add_rate_limit_headers(response, info: dict):
    """Attach standard rate-limit headers to the HTTP response."""
    ul = info.get("user_limit", {})
    il = info.get("ip_limit", {})

    response.headers["X-RateLimit-User-Remaining"] = ul.get("remaining_tokens", "-")
    response.headers["X-RateLimit-User-Capacity"] = ul.get("capacity", "-")
    response.headers["X-RateLimit-IP-Remaining"] = il.get("remaining", "-")
    response.headers["X-RateLimit-IP-Limit"] = il.get("limit", "-")
    response.headers["X-RateLimit-Window"] = il.get("window_size", "-")

    if not info.get("allowed", True):
        response.headers["Retry-After"] = info.get("retry_after", 60)

    return response


class RateLimitMiddleware:
    """
    WSGI-style middleware that wraps a Flask app.
    Intercepts every request before it reaches route handlers.
    """

    def __init__(self, app: Flask):
        cfg = get_config()
        self.cfg = cfg
        self.redis_client = _get_redis_client(cfg)
        self.limiter = HybridRateLimiter(
            self.redis_client,
            user_capacity=cfg.USER_TOKEN_CAPACITY,
            user_refill_rate=cfg.USER_REFILL_RATE,
            ip_limit=cfg.IP_REQUEST_LIMIT,
            ip_window=cfg.IP_WINDOW_SECONDS,
        )

        # Register before/after hooks on the Flask app
        app.before_request(self._before_request)
        app.after_request(self._after_request)

        logger.info(
            "RateLimitMiddleware initialised — user bucket: %d tokens @ %.1f/s | "
            "IP window: %d req / %ds",
            cfg.USER_TOKEN_CAPACITY,
            cfg.USER_REFILL_RATE,
            cfg.IP_REQUEST_LIMIT,
            cfg.IP_WINDOW_SECONDS,
        )

    def _before_request(self):
        if not self.cfg.RATE_LIMIT_ENABLED:
            return

        ip = _get_client_ip()

        # Whitelist bypass (e.g., internal health-check IPs)
        if ip in self.cfg.BYPASS_IPS:
            g.rate_limit_info = {"allowed": True, "bypassed": True}
            return

        user_id = _get_user_id()

        try:
            allowed, info = self.limiter.is_allowed(user_id, ip)
        except redis.RedisError as exc:
            # Fail open: if Redis is down, let the request through but log it
            logger.error("Redis error during rate limit check: %s", exc)
            g.rate_limit_info = {"allowed": True, "redis_error": True}
            return

        g.rate_limit_info = info

        if not allowed:
            logger.warning("Rate limit exceeded — user=%s ip=%s", user_id, ip)
            resp = jsonify(
                {
                    "error": "Too Many Requests",
                    "message": "Rate limit exceeded. Please slow down.",
                    "retry_after": info.get("retry_after", 60),
                    "details": info,
                }
            )
            resp.status_code = 429
            _add_rate_limit_headers(resp, info)
            return resp

    def _after_request(self, response):
        info = getattr(g, "rate_limit_info", {})
        return _add_rate_limit_headers(response, info)


# ---------------------------------------------------------------------------
# Standalone decorator — use on specific routes when you don't want global
# ---------------------------------------------------------------------------

def rate_limit(
    limit: int = 30,
    window: int = 60,
    strategy: str = "sliding_window",
    key_func: Callable | None = None,
):
    """
    Route-level decorator for fine-grained rate limiting.

    Args:
        limit:    Maximum requests per window (sliding_window) or
                  bucket capacity (token_bucket).
        window:   Window size in seconds (sliding_window only).
        strategy: "sliding_window" | "token_bucket"
        key_func: Optional callable(request) → str to derive the rate-limit key.

    Example::

        @app.route("/expensive")
        @rate_limit(limit=5, window=60)
        def expensive_endpoint():
            ...
    """
    cfg = get_config()
    _redis = _get_redis_client(cfg)

    if strategy == "token_bucket":
        limiter = TokenBucketLimiter(_redis, capacity=limit, refill_rate=limit / window)
    else:
        limiter = SlidingWindowLimiter(_redis, limit=limit, window_size=window)

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not cfg.RATE_LIMIT_ENABLED:
                return f(*args, **kwargs)

            identifier = key_func(request) if key_func else _get_user_id()

            try:
                if strategy == "token_bucket":
                    allowed, info = limiter.is_allowed(identifier)
                else:
                    allowed, info = limiter.is_allowed(identifier)
            except redis.RedisError:
                return f(*args, **kwargs)  # fail open

            if not allowed:
                resp = jsonify(
                    {
                        "error": "Too Many Requests",
                        "message": f"Max {limit} requests per {window}s on this endpoint.",
                        "retry_after": info.get("retry_after", window),
                    }
                )
                resp.status_code = 429
                return resp

            return f(*args, **kwargs)

        return wrapper

    return decorator


def init_rate_limiter(app: Flask) -> RateLimitMiddleware:
    """Attach the global rate-limit middleware to a Flask app."""
    return RateLimitMiddleware(app)
