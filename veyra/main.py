"""FastAPI application with lifespan events."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from veyra.api.routes import router
from veyra.db.engine import engine
from veyra.db.models import Base
from veyra.engine.account_manager import manager


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


app = FastAPI(title="Veyra Bot", version="0.1.0", lifespan=lifespan)
app.include_router(router)

web_dir = Path(__file__).parent / "web"


@app.get("/")
async def index():
    return FileResponse(web_dir / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


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
