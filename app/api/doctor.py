"""Preflight check for local development.

Answers the one question that is otherwise painful to debug: *can the browser
actually reach the API from the page it loaded?* The most common local failure is
a port mismatch — Next.js silently increments to 3001 when 3000 is busy, the API
is only reachable on 8000, and the browser blocks the response on CORS. The UI
reports all of that as one vague "Backend unavailable" message.

Run it while both servers are up:

    cd backend && .venv/bin/python -m app.api.doctor
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_DEFAULT = "http://localhost:8000"
APP_DEFAULT = "http://localhost:3000"


def _api_url_from_env_local() -> str | None:
    """Read ``NEXT_PUBLIC_API_URL`` from the frontend's ``.env.local``.

    The browser gets its API address from this file, and the value is inlined
    into the bundle at build time. The doctor must therefore use the same source
    or it will probe a port nothing is listening on and report a false failure
    — which is worse than not checking at all, because it sends you debugging
    the wrong port.
    """
    # Walk up from this file so the path survives a directory move.
    directory = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        directory = os.path.dirname(directory)
        path = os.path.join(directory, ".env.local")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    if key.strip() == "NEXT_PUBLIC_API_URL":
                        return value.strip().strip("\"'") or None
        except OSError:
            return None
    return None



def _probe(
    url: str,
    origin: str | None = None,
    timeout: float = 4.0,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int | None, str]:
    """Return (status, note). A 401/403 still proves the API is reachable."""
    request = urllib.request.Request(url, data=body, method=method)
    if origin:
        request.add_header("Origin", origin)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, "ok"
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code} (reachable, but this endpoint is guarded)"
    except Exception as exc:  # noqa: BLE001 - diagnostic tool, report anything
        return None, f"{type(exc).__name__}: {exc}"


def _cors_allows(api: str, origin: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Ask the server the way a browser does, via a CORS preflight.

    A plain GET is not enough: ``urllib`` ignores the browser's same-origin
    policy, so a GET succeeds even when the browser would discard the response.
    Starlette's CORSMiddleware rejects a preflight from an unlisted origin with
    400 and no ``Access-Control-Allow-Origin`` header, which is exactly the
    condition that produces "Backend unavailable" in the UI.
    """
    request = urllib.request.Request(f"{api}/api/v1/dashboard/summary", method="OPTIONS")
    request.add_header("Origin", origin)
    request.add_header("Access-Control-Request-Method", "GET")
    request.add_header("Access-Control-Request-Headers", "content-type")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            allowed = response.headers.get("Access-Control-Allow-Origin")
            if allowed == origin:
                return True, "preflight accepted"
            return False, f"allow-origin was {allowed!r}"
    except urllib.error.HTTPError as exc:
        allowed = exc.headers.get("Access-Control-Allow-Origin") if exc.headers else None
        if allowed == origin:
            return True, f"preflight accepted (HTTP {exc.code})"
        return False, f"preflight rejected with HTTP {exc.code}, allow-origin {allowed!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    # Explicit override wins, then .env.local (what the browser actually uses),
    # then the conventional default.
    api = (
        os.environ.get("NEXT_PUBLIC_API_URL")
        or _api_url_from_env_local()
        or API_DEFAULT
    )
    api = api.rstrip("/")
    app = os.environ.get("APP_URL") or APP_DEFAULT
    app = app.rstrip("/")

    print("NutriReg AI — local preflight\n")
    print(f"  API from .env.local: {api}")
    print(f"  Frontend origin:    {app}\n")

    problems = 0

    # 1. Is the API listening at all?
    status, note = _probe(f"{api}/health")
    api_port = api.rsplit(":", 1)[-1] if ":" in api.rsplit("//", 1)[-1] else api
    if status == 200:
        print(f"  [OK ] API is healthy at {api}")
    elif status is not None:
        print(f"  [WARN] {api} answered {note}")
    else:
        problems += 1
        print(f"  [FAIL] No API at {api} ({note})")
        print(f"         Start it:  cd backend && DEV_LOGIN_ENABLED=true \\")
        print(
            f"                       .venv/bin/python -m uvicorn app.api.main:app --port {api_port}"
        )


    # 2. Does the allow-list cover the origin the browser will actually send?
    #    Tested with a real preflight, because a plain GET succeeds even when the
    #    browser would throw the response away.
    origin = app
    reachable, reach_note = _probe(f"{api}/health")
    if reachable is None:
        allowed, detail = False, reach_note
    else:
        allowed, detail = _cors_allows(api, origin)
    if allowed:
        print(f"  [OK ] API accepts browser origin {origin}")
    else:
        problems += 1
        print(f"  [FAIL] API rejects browser origin {origin} ({detail})")
        print("         Two usual causes:")
        print("           a) the API was started without that origin in CORS_ORIGINS.")
        print(f"              Fix:  CORS_ORIGINS=\"{origin}\" .venv/bin/python -m uvicorn \\")
        print(f"                    app.api.main:app --port {api_port}")
        print("           b) the frontend is on a different port than the one you expect.")
        print("              Next.js increments the port when 3000 is busy — check the")
        print("              'Local:' line it printed on startup.")

    # 3. Is the frontend serving?
    status, note = _probe(app)
    if status is not None and status < 400:
        print(f"  [OK ] Frontend is serving at {app}")
    elif status is not None:
        print(f"  [WARN] Frontend at {app} answered {note}")
    else:
        print(f"  [-- ] No frontend at {app} ({note}) — optional if you are only testing the API")

    # 4. Is the database reachable?
    if status is not None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import asyncio

            from app.api import db

            async def _check() -> int:
                try:
                    return int(await db.fetch_value("SELECT count(*) FROM regulatory_chunks") or 0)
                finally:
                    await db.close_pool()

            chunks = asyncio.run(_check())
            print(f"  [OK ] Database reachable ({chunks} regulatory chunks loaded)")
            if not chunks:
                print("         Corpus is empty. Load it:")
                print("         .venv/bin/python -m app.api.ingest_regulatory")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"  [FAIL] Database unreachable ({type(exc).__name__}: {exc})")

    # 5. Dev login must be on, or there is no way to obtain a session. Asked of
    #    the running server rather than this process, which is usually a different
    #    shell and therefore a different environment. The body must be valid, or
    #    validation answers 422 before the DEV_LOGIN_ENABLED gate is consulted.
    status, note = _probe(
        f"{api}/api/v1/auth/dev-login",
        method="POST",
        body=json.dumps({"displayName": "Preflight", "email": "preflight@nutrireg.local"}).encode(),
    )
    if status == 200:
        print("  [OK ] API has dev login enabled — a session can be obtained")
    elif status == 404:
        print("  [-- ] Dev login is disabled on the API (the route is not registered).")
        print("         The app has no sign-in screen, so it will show its signed-out")
        print("         state. Restart the API with DEV_LOGIN_ENABLED=true")
    else:
        print(f"  [-- ] Could not determine dev-login state (got {status or note})")

    print()
    if problems:
        print(f"{problems} problem(s) found.")
        return 1
    print("Everything reachable. If a screen still shows an error, hard-reload the")
    print("browser (Cmd+Shift+R) so it stops using a stale bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
