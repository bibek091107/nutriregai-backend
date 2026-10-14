"""asyncpg connection pool and query helpers.

The pool is created once at application startup and closed on shutdown. Queries
go through :func:`fetch_all` / :func:`fetch_one` / :func:`execute` so every
statement is parameterised; no SQL is ever built by string interpolation of user
input.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from typing import Any, Optional

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.api.config import get_settings  # noqa: E402

logger = logging.getLogger("nutrireg.db")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        min_size = int(os.environ.get("DB_POOL_MIN", "1"))
        max_size = int(os.environ.get("DB_POOL_MAX", "10"))
        command_timeout = float(os.environ.get("DB_COMMAND_TIMEOUT", "30.0"))
        connect_timeout = float(os.environ.get("DB_CONNECT_TIMEOUT", "15.0"))

        pool_kwargs: dict[str, Any] = {
            "dsn": settings.database_url,
            "min_size": min_size,
            "max_size": max_size,
            "command_timeout": command_timeout,
            "timeout": connect_timeout,
        }
        if settings.database_ssl is not None:
            pool_kwargs["ssl"] = settings.database_ssl

        try:
            logger.info("Connecting to PostgreSQL pool at %s (ssl=%s)", settings.sanitized_database_target, settings.database_ssl)
            _pool = await asyncpg.create_pool(**pool_kwargs)
            logger.info("PostgreSQL connection pool ready at %s", settings.sanitized_database_target)
        except Exception as exc:
            # Diagnose and classify the connection failure without leaking credentials
            err_str = str(exc)
            sanitized_target = settings.sanitized_database_target

            if isinstance(exc, (ConnectionRefusedError, socket.error)) and (
                "111" in err_str or "61" in err_str or "refused" in err_str.lower()
            ):
                reason = (
                    f"Connection refused at {sanitized_target}. "
                    "The database server is not reachable on this host/port. "
                    "On Render, ensure DATABASE_URL or INTERNAL_DATABASE_URL is set in "
                    "Environment Variables and points to an active PostgreSQL service."
                )
            elif isinstance(exc, socket.gaierror) or "not known" in err_str.lower() or "nodename" in err_str.lower():
                reason = (
                    f"DNS resolution failed for database host in {sanitized_target}. "
                    "Check the database hostname in your Render environment variables."
                )
            elif "password" in err_str.lower() or "authentication" in err_str.lower():
                reason = (
                    f"Database authentication failed for {sanitized_target}. "
                    "Verify the database username and password in your connection string."
                )
            elif "database" in err_str.lower() and "does not exist" in err_str.lower():
                reason = (
                    f"Database specified in {sanitized_target} does not exist. "
                    "Verify the database name in your connection string."
                )
            elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in err_str.lower():
                reason = (
                    f"Connection timed out after {connect_timeout}s connecting to {sanitized_target}. "
                    "Check network access, security groups, or whether the Render database is suspended."
                )
            elif "ssl" in err_str.lower() or "certificate" in err_str.lower():
                reason = (
                    f"SSL/TLS handshake error connecting to {sanitized_target}: {exc}. "
                    "Verify SSL configuration (try ?sslmode=require for external URLs, or disable SSL for internal Render network)."
                )
            else:
                reason = f"Failed to connect to PostgreSQL at {sanitized_target}: {type(exc).__name__}: {exc}"

            logger.error("Database connection failure: %s", reason)
            raise RuntimeError(reason) from exc

    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as connection:
        return await connection.fetch(query, *args)


async def fetch_one(query: str, *args: Any) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as connection:
        return await connection.fetchrow(query, *args)


async def fetch_value(query: str, *args: Any) -> Any:
    row = await fetch_one(query, *args)
    return None if row is None else row[0]


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    async with pool.acquire() as connection:
        return await connection.execute(query, *args)


async def transaction():
    """Context manager yielding a connection inside a transaction."""
    pool = await get_pool()
    return pool.acquire()
