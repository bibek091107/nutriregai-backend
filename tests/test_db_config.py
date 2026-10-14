"""Tests for database URL parsing, SSL detection, and error categorization."""

import asyncio
import os
import pytest
from unittest.mock import patch, AsyncMock
from app.api.config import sanitize_db_url, resolve_database_config, Settings
from app.api.health import liveness, readiness, health
from fastapi import Response, status


def test_sanitize_db_url_strips_passwords():
    # Passwords should be replaced with ***
    url = "postgresql://user:secret_password@dpg-c12345-a.render.com:5432/nutrireg_db?sslmode=require"
    sanitized = sanitize_db_url(url)
    assert "secret_password" not in sanitized
    assert "user:***@" in sanitized
    assert "dpg-c12345-a.render.com:5432/nutrireg_db" in sanitized


def test_sanitize_db_url_no_password():
    url = "postgresql://localhost:5432/nutrireg"
    sanitized = sanitize_db_url(url)
    assert sanitized == "postgresql://localhost:5432/nutrireg"


def test_sanitize_db_url_handles_empty_or_invalid():
    assert sanitize_db_url("") == "<empty>"
    assert sanitize_db_url(None) == "<empty>"


def test_resolve_database_config_internal_url_priority():
    env = {
        "INTERNAL_DATABASE_URL": "postgres://user:pass@dpg-internal-host:5432/nutrireg",
        "DATABASE_URL": "postgresql://user:pass@external.render.com:5432/nutrireg",
    }
    with patch.dict(os.environ, env, clear=True):
        dsn, ssl = resolve_database_config()
        # Scheme normalized to postgresql://
        assert dsn.startswith("postgresql://")
        assert "dpg-internal-host" in dsn
        # Render internal host has no dots, SSL defaults to False
        assert ssl is False


def test_resolve_database_config_external_url_ssl_auto():
    env = {
        "DATABASE_URL": "postgres://user:pass@dpg-c123-a.oregon-postgres.render.com:5432/nutrireg",
    }
    with patch.dict(os.environ, env, clear=True):
        dsn, ssl = resolve_database_config()
        assert dsn.startswith("postgresql://")
        # External host with domain requires SSL by default
        assert ssl == "require"


def test_resolve_database_config_query_param_ssl():
    env = {
        "DATABASE_URL": "postgresql://user:pass@external.com:5432/nutrireg?sslmode=disable",
    }
    with patch.dict(os.environ, env, clear=True):
        dsn, ssl = resolve_database_config()
        assert ssl is False


def test_resolve_database_config_missing_on_render_raises():
    env = {
        "RENDER": "true",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError) as exc_info:
            resolve_database_config()
        assert "Missing required PostgreSQL database configuration on Render" in str(exc_info.value)
        assert "INTERNAL_DATABASE_URL" in str(exc_info.value)


def test_resolve_database_config_local_dev_fallback():
    # In local development without RENDER, falls back to local postgres
    with patch.dict(os.environ, {}, clear=True):
        dsn, ssl = resolve_database_config()
        assert "localhost:5432/nutrireg" in dsn
        assert ssl is False


def test_health_liveness_endpoint():
    res = asyncio.run(liveness())
    assert res == {"status": "ok", "live": True}


def test_health_readiness_endpoint_success():
    resp = Response()
    with patch("app.api.db.fetch_value", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = 1
        res = asyncio.run(readiness(resp))
        assert res == {"status": "ok", "database": True}
        assert resp.status_code == status.HTTP_200_OK


def test_health_readiness_endpoint_failure():
    resp = Response()
    with patch("app.api.db.fetch_value", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = Exception("Connection refused")
        res = asyncio.run(readiness(resp))
        assert res == {"status": "unavailable", "database": False}
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_health_general_endpoint_database_down():
    resp = Response()
    with patch("app.api.db.fetch_value", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = Exception("Connection refused")
        res = asyncio.run(health(resp))
        assert res == {"status": "degraded", "database": False}
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_pool_connection_refused_categorized_error():
    from app.api import db
    db._pool = None
    with patch("asyncpg.create_pool", side_effect=ConnectionRefusedError("[Errno 111] Connection refused")):
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(db.get_pool())
        err = str(exc_info.value)
        assert "Connection refused" in err
        assert "On Render, ensure DATABASE_URL or INTERNAL_DATABASE_URL is set" in err
        assert "***" in err or "password" not in err  # credentials masked

