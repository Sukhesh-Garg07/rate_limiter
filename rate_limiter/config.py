"""
Configuration for the Distributed Rate Limiter.
Values are read from environment variables with safe defaults.
"""

import os


class Config:
    # ── Redis ──────────────────────────────────────────────────────────────
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB: int = int(os.getenv("REDIS_DB", 0))
    REDIS_PASSWORD: str | None = os.getenv("REDIS_PASSWORD", None)
    REDIS_SOCKET_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", 1.0))

    # ── Token Bucket (per-user) ────────────────────────────────────────────
    USER_TOKEN_CAPACITY: int = int(os.getenv("USER_TOKEN_CAPACITY", 20))
    USER_REFILL_RATE: float = float(os.getenv("USER_REFILL_RATE", 2.0))   # tokens/sec

    # ── Sliding Window (per-IP) ────────────────────────────────────────────
    IP_REQUEST_LIMIT: int = int(os.getenv("IP_REQUEST_LIMIT", 100))
    IP_WINDOW_SECONDS: int = int(os.getenv("IP_WINDOW_SECONDS", 60))

    # ── Flask App ──────────────────────────────────────────────────────────
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")

    # ── Misc ───────────────────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    BYPASS_IPS: list[str] = [
        ip.strip()
        for ip in os.getenv("BYPASS_IPS", "127.0.0.1").split(",")
        if ip.strip()
    ]


class DevelopmentConfig(Config):
    DEBUG = True
    USER_TOKEN_CAPACITY = 50
    IP_REQUEST_LIMIT = 500


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    REDIS_DB = 15          # isolated test database
    USER_TOKEN_CAPACITY = 5
    USER_REFILL_RATE = 1.0
    IP_REQUEST_LIMIT = 10
    IP_WINDOW_SECONDS = 10


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}

def get_config() -> type[Config]:
    env = os.getenv("APP_ENV", "development")
    return config_map.get(env, DevelopmentConfig)
