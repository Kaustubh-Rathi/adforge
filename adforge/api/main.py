"""AdForge FastAPI main app."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from adforge.api.routes import campaigns, kpis, reviews, traces
from adforge.config import get_settings
from adforge.storage.redis_client import close_redis
from adforge.storage.session import dispose_engine, init_engine

logger = logging.getLogger("adforge.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise / dispose singletons on app start / stop."""
    settings = get_settings()
    init_engine(echo=settings.app_env == "dev")
    logger.info("AdForge API starting (env=%s, llm=%s)", settings.app_env, settings.llm_provider)
    yield
    await dispose_engine()
    await close_redis()
    logger.info("AdForge API stopped")


app = FastAPI(
    title="AdForge",
    description="End-to-end multi-agent ad campaign platform",
    version="0.1.0",
    lifespan=lifespan,
)

_settings = get_settings()
_origins = [
    origin.strip()
    for origin in _settings.app_cors_origins.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=bool(_origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-AdForge-Tenant", "X-AdForge-Campaign",
                   "X-AdForge-Reviewer", "X-API-Key"],
)

app.include_router(campaigns.router)
app.include_router(kpis.router)
app.include_router(reviews.router)
app.include_router(traces.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "name": "AdForge API",
        "docs": "/docs",
        "version": "0.1.0",
        "agents": [
            "research",
            "creative_strategist",
            "media_planner",
            "kpi_analytics",
            "roi_calculator",
            "compliance",
            "optimization",
        ],
    }


def run() -> None:
    """Entry point for the `adforge-api` console script."""
    settings = get_settings()
    uvicorn.run(
        "adforge.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env == "dev",
    )


if __name__ == "__main__":
    run()
