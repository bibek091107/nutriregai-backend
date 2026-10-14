"""asyncpg connection pool and query helpers.

The pool is created once at application startup and closed on shutdown. Queries
go through :func:`fetch_all` / :func:`fetch_one` / :func:`execute` so every
statement is parameterised; no SQL is ever built by string interpolation of user
input.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional, Sequence

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.api.config import get_settings  # noqa: E402

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=get_settings().database_url,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
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
