"""API service configuration.

Every setting comes from the environment so nothing is baked into the image.
The database password is read from ``DATABASE_URL`` and never logged, echoed or
returned by any endpoint.
"""

from __future__ import annotations

import os
from functools import lru_cache

# Cookie the session token travels in. Deliberately NOT a __Host- prefix cookie
# during local development, because that requires Secure and therefore HTTPS.
SESSION_COOKIE = "nutrireg_session"
SESSION_TTL_HOURS = 12

# Browsers only send a cross-origin cookie when the API opts in to CORS and the
# request carries credentials. Both are required for cookie auth from the
# Next.js dev server.
#
# Read from CORS_ORIGINS as a comma-separated list so a frontend that lands on a
# different port (Next auto-increments when 3000 is busy) can be allowed without
# a code change:  CORS_ORIGINS="http://localhost:3000,http://localhost:3001"
CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
)

# Local uploads live under the backend package so they never leave the repo.
UPLOAD_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
)


class Settings:
    def __init__(self) -> None:
        self.database_url: str = os.environ.get(
            "DATABASE_URL",
            "postgresql://nutrireg:nutrireg_local_dev@localhost:5432/nutrireg",
        )
        self.cors_origins: tuple[str, ...] = CORS_ORIGINS
        self.session_ttl_hours: int = int(
            os.environ.get("SESSION_TTL_HOURS", str(SESSION_TTL_HOURS))
        )
        default_upload_dir = (
            "/tmp/uploads" if os.environ.get("VERCEL") else UPLOAD_DIR
        )
        self.upload_dir: str = os.environ.get("UPLOAD_DIR", default_upload_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
