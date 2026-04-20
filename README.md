# Distributed Rate Limiter

A production-grade distributed rate limiter built with **Python**, **Flask**, and **Redis**. Implements both the **Token Bucket** and **Sliding Window** algorithms using Redis Lua scripts for atomically-safe concurrent rate limiting across distributed API servers.

---

## Features

- **Token Bucket** — per-user burst control with smooth long-term rate enforcement
- **Sliding Window** — per-IP precise request counting with no boundary-spike problem
- **Hybrid Limiter** — combines both strategies; a request must pass both checks
- **Redis Lua atomicity** — no race conditions even under heavy concurrent load
- **Configurable** — all limits tunable via environment variables
- **Graceful fail-open** — if Redis is unreachable, requests pass through (logged)
- **Standard headers** — `X-RateLimit-*` and `Retry-After` on every response
- **Route-level decorator** — `@rate_limit()` for per-endpoint overrides

---

## Architecture

```
Client Request
      │
      ▼
Flask Before-Request Hook
      │
      ├─── Token Bucket Check (per User ID / API Key)
      │         └── Redis HMGET/HMSET via Lua script (atomic)
      │
      ├─── Sliding Window Check (per IP address)
      │         └── Redis ZADD/ZREMRANGEBYSCORE/ZCARD via Lua script (atomic)
      │
      ├── BOTH pass? → Route Handler → Response + X-RateLimit-* headers
      └── Either fails? → 429 Too Many Requests + Retry-After header
```

---

## Quick Start

### 1. Start Redis

```bash
docker-compose up redis -d
```

Or install Redis locally: https://redis.io/docs/getting-started/

### 2. Install dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env to adjust limits, Redis connection, etc.
```

### 4. Run the app

```bash
python app.py
# → http://localhost:5000
```

---

## Run with Docker (full stack)

```bash
docker-compose up --build
```

---

## Endpoints

| Method | Path            | Description                               |
|--------|-----------------|-------------------------------------------|
| GET    | `/`             | Health check (not rate-limited)           |
| GET    | `/api/public`   | Public endpoint (global hybrid limiter)   |
| GET    | `/api/data`     | Extra 30 req/min sliding window           |
| POST   | `/api/process`  | Extra 5-burst token bucket                |
| GET    | `/api/status`   | Live rate-limit counters for caller       |
| GET    | `/admin/stats`  | Aggregate Redis key counts + memory info  |

---

## Test the Rate Limiter

### Manual (httpie or curl)

```bash
# Simulate a user making rapid requests
for i in $(seq 1 25); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-User-ID: alice" \
    http://localhost:5000/api/public
done
# First 20 → 200, remaining → 429
```

### Check your live counters

```bash
curl -H "X-User-ID: alice" http://localhost:5000/api/status | python -m json.tool
```

### Run the automated test suite

```bash
# Requires Redis on localhost:6379 (DB 15 is used — isolated)
APP_ENV=testing pytest test_rate_limiter.py -v
```

---

## Configuration Reference

| Variable              | Default     | Description                            |
|-----------------------|-------------|----------------------------------------|
| `REDIS_HOST`          | `localhost` | Redis hostname                         |
| `REDIS_PORT`          | `6379`      | Redis port                             |
| `REDIS_DB`            | `0`         | Redis database index                   |
| `REDIS_PASSWORD`      | *(none)*    | Redis auth password                    |
| `USER_TOKEN_CAPACITY` | `20`        | Max tokens per user bucket             |
| `USER_REFILL_RATE`    | `2.0`       | Tokens added per second per user       |
| `IP_REQUEST_LIMIT`    | `100`       | Max requests per IP per window         |
| `IP_WINDOW_SECONDS`   | `60`        | Sliding window duration (seconds)      |
| `RATE_LIMIT_ENABLED`  | `true`      | Master on/off switch                   |
| `BYPASS_IPS`          | `127.0.0.1` | Comma-separated IPs to skip limiting   |
| `APP_ENV`             | `development`| `development` / `production` / `testing` |

---

## How the Algorithms Work

### Token Bucket (per user)

Each user has a "bucket" of tokens. Every request consumes one token. Tokens refill at `USER_REFILL_RATE` per second, capped at `USER_TOKEN_CAPACITY`. This allows short bursts while enforcing a long-term average rate. The state (`tokens`, `last_refill`) is stored in a Redis hash and updated atomically via a Lua script.

### Sliding Window (per IP)

Every request timestamp is added to a Redis sorted set keyed by IP. Before each check, entries older than `IP_WINDOW_SECONDS` are removed. If the remaining count ≥ `IP_REQUEST_LIMIT`, the request is rejected. Unlike fixed windows, this approach has no boundary-spike problem — the window slides continuously.

### Why Lua Scripts?

Both algorithms use `EVAL` to run Lua scripts inside Redis. This makes the read-modify-write sequence **atomic** — no two concurrent requests can interleave and both slip through a limit check simultaneously.

---

## Project Structure

```
distributed-rate-limiter/
├── rate_limiter/
│   ├── __init__.py          # Public API exports
│   ├── algorithms.py        # TokenBucket, SlidingWindow, HybridRateLimiter
│   ├── middleware.py        # Flask before/after hooks + @rate_limit decorator
│   └── config.py            # Environment-based configuration
├── app.py                   # Flask demo application
├── test_rate_limiter.py     # pytest test suite
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Response Headers

Every response includes rate-limit metadata:

```
X-RateLimit-User-Remaining: 17
X-RateLimit-User-Capacity:  20
X-RateLimit-IP-Remaining:   83
X-RateLimit-IP-Limit:       100
X-RateLimit-Window:         60
Retry-After: 12              ← only on 429 responses
```
