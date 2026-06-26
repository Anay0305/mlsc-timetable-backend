"""Process-wide SlowAPI limiter.

Single instance so all routers share the same in-memory bucket. For multi-
worker deployments swap the storage URI to Redis (see slowapi docs); the rest
of the API surface stays the same.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


def _key(request) -> str:
    """Rate-limit key: prefer X-User-Id when present, else client IP.

    This means even a single shared NAT IP isn't unfairly throttled while a
    well-behaved client is identified, and missing/spoofed user ids still fall
    back to the connection IP.
    """
    user_id = request.headers.get("X-User-Id")
    if user_id:
        return f"uid:{user_id}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_key, headers_enabled=True)
