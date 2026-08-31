"""FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, db, logging_conf, migrations, store
from .config import APP_NAME
from .engine import scheduler
from .web.api import router as api_router
from .web.pages import router as pages_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging_conf.configure(to_file=bool(store.get_config("log_to_file")))
    log.info("%s %s starting", APP_NAME, __version__)
    log.info("Config directory: %s", config.CONFIG_DIR)
    log.info("Database schema version %s", migrations.SCHEMA_VERSION)

    db.connect()
    store.ensure_bootstrap()

    scheduler.start()
    if store.get_config("setup_complete"):
        log.info("Ready. Next scheduled run: %s", scheduler.next_run_time() or "not scheduled")
    else:
        log.info("Ready. Open the web UI to connect to Plex.")

    if store.get_config("safe_mode"):
        log.info(
            "SAFE MODE is ON. Runs will calculate what would change and modify "
            "nothing in Plex."
        )
    if not store.get_config("ui_password_hash"):
        log.warning(
            "No web UI password is set. Anyone who can reach port %s can change "
            "your Plex watch history -- set one in Settings, and keep this port "
            "off the internet.",
            config.PORT,
        )

    try:
        yield
    finally:
        scheduler.shutdown()
        db.close()
        log.info("Stopped.")


app = FastAPI(
    title=APP_NAME,
    version=__version__,
    lifespan=lifespan,
    # No interactive docs: this is an appliance, and the schema is documented in
    # docs/API.md instead.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# The session secret is generated once and kept in the database, so sessions
# survive a container restart instead of logging everyone out.
#
# TRAP: this runs at import time, because SessionMiddleware needs the secret at
# construction. Any tooling or build step that imports app.main must set
# CONFIG_DIR to a throwaway path, or it bakes a shared secret and a stray
# database into wherever it ran.
db.connect()
store.ensure_bootstrap()
app.add_middleware(
    SessionMiddleware,
    secret_key=str(store.get_config("session_secret")),
    session_cookie="unwatcharr",
    same_site="lax",
    https_only=bool(store.get_config("secure_cookies")),
    max_age=30 * 86400,
)

STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(pages_router)
app.include_router(api_router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    """Liveness only, and deliberately unauthenticated so Docker can use it.

    It reveals nothing beyond "the process is up"; anything with real
    information lives behind auth at /api/status.
    """
    return PlainTextResponse("ok")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """API errors are JSON with a `detail` key; pages get HTML."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return HTMLResponse(
            "<h1>404</h1><p>No such page. <a href=\"/\">Back to the dashboard</a>.</p>",
            status_code=404,
        )
    return HTMLResponse(
        f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 body is a nested structure the UI cannot show.
    Flatten it to the same `detail` string shape as every other API error."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query"))
    message = first.get("msg", "Invalid request.")
    detail = f"{field}: {message}" if field else message
    return JSONResponse({"detail": detail}, status_code=422)
