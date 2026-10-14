"""Vercel entrypoint for NutriReg AI FastAPI backend."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.api.main import app

__all__ = ["app"]
