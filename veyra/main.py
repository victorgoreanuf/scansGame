"""FastAPI application with lifespan events."""

import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from veyra.api.routes import router
from veyra.config import settings
from veyra.db.engine import engine
from veyra.db.models import Base
from veyra.engine.account_manager import manager

# ── File logging setup ────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "veyra.log"

_log_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Rotate at 5 MB, keep 3 backups (≈20 MB max)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(_log_fmt)
_file_handler.setLevel(logging.WARNING)

# Root logger: send everything to the file (guard against reload duplicates)
_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _root.handlers):
    _root.addHandler(_file_handler)

# Quiet noisy third-party loggers
for _name in ("httpx", "httpcore", "hpack", "urllib3", "asyncio", "aiosqlite"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Uvicorn loggers: INFO for errors/startup, but filter out polling spam
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)


# Filter out noisy polling endpoints from access logs
class _QuietAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/api/status" not in msg and "/api/logs" not in msg


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await manager.cleanup()
    await engine.dispose()


class DocsGuard(BaseHTTPMiddleware):
    """Block /docs, /redoc, /openapi.json unless the correct key is provided.

    Once the key is validated on /docs, it's stored in a cookie so that
    subsequent fetches (like /openapi.json from the Swagger UI) work too.
    """

    PROTECTED = ("/docs", "/redoc", "/openapi.json")
    COOKIE_NAME = "veyra_docs"

    async def dispatch(self, request: Request, call_next):
        if any(request.url.path == p for p in self.PROTECTED):
            key = settings.docs_key
            if key:
                q_key = request.query_params.get("key", "")
                c_key = request.cookies.get(self.COOKIE_NAME, "")
                if q_key != key and c_key != key:
                    return JSONResponse({"detail": "Not found"}, status_code=404)

                # Set cookie so /openapi.json works after /docs?key=...
                response = await call_next(request)
                if q_key == key:
                    response.set_cookie(
                        self.COOKIE_NAME, key,
                        httponly=True, samesite="lax", max_age=86400,
                    )
                return response
        return await call_next(request)


app = FastAPI(title="Veyra Bot", version="0.1.0", lifespan=lifespan)
app.add_middleware(DocsGuard)
app.include_router(router)

web_dir = Path(__file__).parent / "web"


@app.get("/")
async def index():
    return FileResponse(web_dir / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/logs/file")
async def logs_file(request: Request):
    """Serve the application log file as plain text. Protected by docs_key."""
    key = settings.docs_key
    if key:
        q_key = request.query_params.get("key", "")
        c_key = request.cookies.get(DocsGuard.COOKIE_NAME, "")
        if q_key != key and c_key != key:
            return JSONResponse({"detail": "Not found"}, status_code=404)

    lines = int(request.query_params.get("lines", "500"))
    lines = min(lines, 10000)

    if not LOG_FILE.exists():
        return PlainTextResponse("(no log file yet)\n")

    all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return PlainTextResponse("\n".join(tail) + "\n")


if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


def main():
    import uvicorn
    from veyra.config import settings

    uvicorn.run(
        "veyra.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        reload_dirs=["veyra"],
    )


if __name__ == "__main__":
    main()
