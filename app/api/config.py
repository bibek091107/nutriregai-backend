"""API service configuration.

Every setting comes from the environment so nothing is baked into the image.
Database connection parameters are read from environment variables (supporting
Render's ``INTERNAL_DATABASE_URL`` and ``DATABASE_URL``) and sanitized so
credentials are never logged, echoed, or returned by any endpoint.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from functools import lru_cache
from typing import Optional, Tuple

logger = logging.getLogger("nutrireg.config")

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


def sanitize_db_url(url: str) -> str:
    """Return a sanitized database URL with passwords stripped for safe logging."""
    if not url:
        return "<empty>"
    try:
        parsed = urllib.parse.urlsplit(url)
        user = parsed.username or ""
        auth = f"{user}:***@" if parsed.password else (f"{user}@" if user else "")
        port = f":{parsed.port}" if parsed.port else ""
        host = parsed.hostname or "unknown"
        path = parsed.path or ""
        scheme = parsed.scheme or "postgresql"
        return f"{scheme}://{auth}{host}{port}{path}"
    except Exception:
        return "<invalid-url>"


def resolve_database_config() -> Tuple[str, Optional[bool | str]]:
    """Resolve and validate PostgreSQL configuration from environment variables.

    Priority order for URL discovery:
      1. INTERNAL_DATABASE_URL (Render private network URL)
      2. DATABASE_URL (Standard Render / platform connection string)
      3. EXTERNAL_DATABASE_URL (External connection fallback)
      4. Discrete parameters: DB_HOST / PGHOST, DB_USER, DB_PASSWORD, DB_NAME, DB_PORT

    Returns:
      (normalized_dsn, ssl_setting)
    """
    raw_url = (
        os.environ.get("INTERNAL_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("EXTERNAL_DATABASE_URL")
    )

    if not raw_url:
        host = os.environ.get("DB_HOST") or os.environ.get("PGHOST")
        if host:
            user = os.environ.get("DB_USER") or os.environ.get("PGUSER") or "postgres"
            password = os.environ.get("DB_PASSWORD") or os.environ.get("PGPASSWORD") or ""
            port = os.environ.get("DB_PORT") or os.environ.get("PGPORT") or "5432"
            dbname = os.environ.get("DB_NAME") or os.environ.get("PGDATABASE") or "nutrireg"
            auth = f"{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@" if password else f"{urllib.parse.quote_plus(user)}@"
            raw_url = f"postgresql://{auth}{host}:{port}/{dbname}"

    is_managed_env = bool(
        os.environ.get("RENDER")
        or os.environ.get("VERCEL")
        or os.environ.get("ENVIRONMENT", "").lower() in {"production", "staging"}
        or os.environ.get("NODE_ENV", "").lower() == "production"
    )

    if not raw_url:
        if is_managed_env:
            raise RuntimeError(
                "Missing required PostgreSQL database configuration on Render / production. "
                "Neither 'INTERNAL_DATABASE_URL' nor 'DATABASE_URL' is set in the environment. "
                "Please provision a PostgreSQL database on Render and configure DATABASE_URL."
            )
        # Local development fallback
        raw_url = "postgresql://nutrireg:nutrireg_local_dev@localhost:5432/nutrireg"

    # Normalize scheme (e.g. postgres:// -> postgresql://)
    if raw_url.startswith("postgres://"):
        raw_url = "postgresql://" + raw_url[len("postgres://"):]

    # Parse and determine SSL configuration
    parsed = urllib.parse.urlsplit(raw_url)
    query_params = urllib.parse.parse_qs(parsed.query)

    explicit_ssl = os.environ.get("DB_SSL") or os.environ.get("DB_SSLMODE") or os.environ.get("PGSSLMODE")
    hostname = (parsed.hostname or "").lower()

    # Determine SSL setting for asyncpg
    ssl_setting: Optional[bool | str] = None

    if explicit_ssl:
        s = explicit_ssl.strip().lower()
        if s in {"1", "true", "yes", "require"}:
            ssl_setting = "require"
        elif s in {"0", "false", "no", "disable"}:
            ssl_setting = False
        else:
            ssl_setting = s
    elif "sslmode" in query_params:
        mode = query_params["sslmode"][0].lower()
        if mode in {"disable", "allow"}:
            ssl_setting = False
        elif mode in {"require", "prefer"}:
            ssl_setting = "require"
        else:
            ssl_setting = mode
    else:
        # Auto-detect based on host: Render internal hosts (e.g. dpg-xxx or .internal) and localhost don't use SSL.
        # External hosts (.render.com, .neon.tech, .supabase.co, etc.) require SSL.
        if hostname in {"localhost", "127.0.0.1", "::1"} or (
            hostname.startswith("dpg-") and "." not in hostname
        ) or hostname.endswith(".render.internal"):
            ssl_setting = False
        elif "." in hostname and not hostname.endswith(".internal"):
            ssl_setting = "require"

    return raw_url, ssl_setting


class Settings:
    def __init__(self) -> None:
        self.database_url, self.database_ssl = resolve_database_config()
        self.sanitized_database_target: str = sanitize_db_url(self.database_url)
        self.cors_origins: tuple[str, ...] = CORS_ORIGINS
        self.session_ttl_hours: int = int(
            os.environ.get("SESSION_TTL_HOURS", str(SESSION_TTL_HOURS))
        )
        default_upload_dir = (
            "/tmp/uploads" if (os.environ.get("VERCEL") or os.environ.get("RENDER")) else UPLOAD_DIR
        )
        self.upload_dir: str = os.environ.get("UPLOAD_DIR", default_upload_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
