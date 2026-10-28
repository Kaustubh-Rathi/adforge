"""AdForge FastAPI main app."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from adforge.api.routes import campaigns, kpis, reviews, traces
from adforge.config import get_settings
from adforge.storage.session import dispose_engine, init_engine
from adforge.storage.redis_client import close_redis

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns.router)
app.include_router(kpis.router)
app.include_router(reviews.router)
app.include_router(traces.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": "AdForge API",
        "docs": "/docs",
        "version": "0.1.0",
        "agents": "research, creative_strategist, media_planner, kpi_analytics, roi_calculator, compliance, optimization",
    }


def run() -> None:
    """Entry point for the `adforge-api` console script."""
    settings = get_settings()
    uvicorn.run(
        "adforge.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "dev",
    )


if __name__ == "__main__":
    run()
