"""
Distributed Rate Limiter — Demo Flask Application
===================================================
Demonstrates the rate limiter protecting four kinds of endpoints:

  GET  /                      → health check (not rate-limited)
  GET  /api/public            → global hybrid limiter applies
  GET  /api/data              → extra per-route sliding-window limit (30/min)
  POST /api/process           → strict per-route token-bucket (5 burst)
  GET  /api/status            → real-time stats for the calling user/IP
  GET  /admin/stats           → aggregate Redis key metrics
"""

import logging
import os
import time

import redis
from flask import Flask, jsonify, request

from rate_limiter import init_rate_limiter, rate_limit
from rate_limiter.config import get_config
from rate_limiter.middleware import _get_client_ip, _get_redis_client, _get_user_id

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
cfg = get_config()
app.config["SECRET_KEY"] = cfg.SECRET_KEY

# Attach global hybrid rate limiter (token bucket per user + sliding window per IP)
init_rate_limiter(app)

# Shared Redis client for stats queries
_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = _get_redis_client(cfg)
    return _redis_client


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def health():
    """Health check — intentionally excluded from rate limiting."""
    return jsonify(
        {
            "status": "ok",
            "service": "Distributed Rate Limiter",
            "redis": _redis_ping(),
            "timestamp": time.time(),
        }
    )


@app.route("/api/public")
def public_endpoint():
    """
    Standard public endpoint — protected by the global hybrid limiter only.
    Limits: 20 token-bucket capacity per user, 100 requests/60s per IP.
    """
    return jsonify(
        {
            "message": "Public endpoint response",
            "user": _get_user_id(),
            "ip": _get_client_ip(),
            "timestamp": time.time(),
        }
    )


@app.route("/api/data")
@rate_limit(limit=30, window=60, strategy="sliding_window")
def data_endpoint():
    """
    Data endpoint — global limiter + additional 30 req/min sliding window.
    Simulates a moderate-cost database query.
    """
    # Simulate some work
    time.sleep(0.01)
    return jsonify(
        {
            "data": [{"id": i, "value": i * 2} for i in range(10)],
            "count": 10,
            "timestamp": time.time(),
        }
    )


@app.route("/api/process", methods=["POST"])
@rate_limit(limit=5, window=60, strategy="token_bucket")
def process_endpoint():
    """
    CPU-heavy endpoint — global limiter + tight token bucket (5 burst, ~1 req/12s).
    Simulates an expensive computation or ML inference call.
    """
    payload = request.get_json(silent=True) or {}
    time.sleep(0.05)   # simulate work
    return jsonify(
        {
            "result": "processed",
            "input_keys": list(payload.keys()),
            "timestamp": time.time(),
        }
    )


@app.route("/api/status")
def status_endpoint():
    """Return live rate-limit counters for the calling user and IP."""
    r = get_redis()
    user_id = _get_user_id()
    ip = _get_client_ip()

    user_key = f"user_tb:{user_id}"
    ip_key = f"ip_sw:{ip}"

    # Token bucket state
    tb_data = r.hgetall(user_key) or {}
    tb_tokens = round(float(tb_data.get("tokens", cfg.USER_TOKEN_CAPACITY)), 2)

    # Sliding window count
    now_ms = int(time.time() * 1000)
    window_start = now_ms - (cfg.IP_WINDOW_SECONDS * 1000)
    ip_count = r.zcount(ip_key, window_start, "+inf")

    return jsonify(
        {
            "user_id": user_id,
            "ip_address": ip,
            "user_token_bucket": {
                "current_tokens": tb_tokens,
                "capacity": cfg.USER_TOKEN_CAPACITY,
                "refill_rate_per_sec": cfg.USER_REFILL_RATE,
            },
            "ip_sliding_window": {
                "requests_in_window": ip_count,
                "limit": cfg.IP_REQUEST_LIMIT,
                "window_seconds": cfg.IP_WINDOW_SECONDS,
                "remaining": max(0, cfg.IP_REQUEST_LIMIT - ip_count),
            },
        }
    )


@app.route("/admin/stats")
def admin_stats():
    """Aggregate stats: active rate-limit keys and Redis memory usage."""
    r = get_redis()

    tb_keys = r.keys("user_tb:*")
    sw_keys = r.keys("ip_sw:*")

    info = r.info("memory")

    return jsonify(
        {
            "active_user_buckets": len(tb_keys),
            "active_ip_windows": len(sw_keys),
            "redis_used_memory": info.get("used_memory_human"),
            "config": {
                "user_capacity": cfg.USER_TOKEN_CAPACITY,
                "user_refill_rate": cfg.USER_REFILL_RATE,
                "ip_limit": cfg.IP_REQUEST_LIMIT,
                "ip_window": cfg.IP_WINDOW_SECONDS,
            },
        }
    )


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found", "path": request.path}), 404


@app.errorhandler(500)
def server_error(e):
    logger.exception("Internal server error")
    return jsonify({"error": "Internal Server Error"}), 500


# ── Helpers ───────────────────────────────────────────────────────────────────

def _redis_ping() -> str:
    try:
        get_redis().ping()
        return "connected"
    except redis.RedisError:
        return "unavailable"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info("Starting server on port %d (env=%s)", port, os.getenv("APP_ENV", "development"))
    app.run(host="0.0.0.0", port=port, debug=cfg.DEBUG)
