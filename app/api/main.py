"""FastAPI application factory.

Startup opens the database pool; shutdown closes it. Routers are mounted under
``/api/v1`` to match the paths the frontend client already requests, so no
frontend change is required to connect it.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.api import db  # noqa: E402
from app.api.auth_routes import router as auth_router  # noqa: E402
from app.api.config import get_settings  # noqa: E402
from app.api.documents import router as documents_router  # noqa: E402
from app.api.health import router as health_router  # noqa: E402
from app.api.products import router as products_router  # noqa: E402
from app.api.regulatory import router as regulatory_router  # noqa: E402

logger = logging.getLogger("nutrireg.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    # Log the target without the password: a connection string can contain
    # credentials, and logs outlive the process.
    logger.info("connecting to database at %s", settings.database_url.split("@")[-1])
    await db.get_pool()
    logger.info("database pool ready")
    _log_connection_summary(settings)
    yield
    await db.close_pool()
    logger.info("database pool closed")


def _log_connection_summary(settings) -> None:
    """State the allowed origins on startup.

    The dominant local failure is a port mismatch: Next.js silently increments
    to 3001 when 3000 is busy, the browser's request is then rejected by CORS,
    and the console shows a wall of "not allowed by Access-Control-Allow-Origin"
    errors that name the origin but not the remedy. Printing the allow-list at
    startup makes the mismatch obvious at a glance.
    """
    allowed = ", ".join(settings.cors_origins)
    print(f"\n  NutriReg API is running.")
    print(f"  Allowed browser origins: {allowed}")
    print(
        "  If the browser reports an origin is not allowed, either start the\n"
        "  frontend on one of those ports, or restart this API with:\n"
        "      CORS_ORIGINS=\"http://localhost:3001\" ...\n"
        "  Diagnose with:  .venv/bin/python -m app.api.doctor\n",
        flush=True,
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="NutriReg AI API",
        version="0.1.0",
        description=(
            "Backend for NutriReg AI. Serves the product dossier API and the "
            "preprocessed FSSAI statutory corpus."
        ),
        lifespan=lifespan,
    )

    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            *settings.cors_origins,
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
        ],
        allow_origin_regex=r"https://.*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(products_router)
    app.include_router(regulatory_router)
    app.include_router(documents_router)
    return app


app = create_app()
