"""
main.py — Stock Data Features Service
Standalone FastAPI microservice for calculating technical indicators.
Triggered via POST /features/calculate from stock-data-node or manually.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sys
from pathlib import Path

from typing import Optional


import uvicorn
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, StreamingResponse

# ─── Bootstrap: ensure src/ is on the path ──────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from calculator import TechnicalCalculator
from config_parser import FeatureConfigParser, ProcessingContext, FeatureType
from parquet_io import ParquetStorage
from processor import FeatureProcessor
from job_manager import JobManager
from logging_setup import configure_logging




# ─── Config ──────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

BASE_DIR = Path(os.environ.get("APP_BASE_DIR", Path(__file__).parent.parent))
CONFIG_DIR = str(BASE_DIR / "config")
LOG_DIR = str(BASE_DIR / "logs")
DATA_DIR = str(BASE_DIR / "data" / "parquet")
API_PORT = int(os.environ.get("FEATURES_API_PORT", "8003"))


def _load_settings() -> dict:
    """Load settings.json for processing_threads etc."""
    settings_path = Path(CONFIG_DIR) / "settings.json"
    if settings_path.exists():
        with open(settings_path, "r") as f:
            return json.load(f)
    return {}


# ─── Feature Pipeline Runner ────────────────────────────────────

def run_feature_pipeline(log_queue: Optional[multiprocessing.Queue] = None, priority: Optional[str] = None) -> None:
    """Runs the full feature calculation pipeline (blocking).
    If priority is set, that ticker is processed first."""
    # Import inside to ensure availability in background threads and avoid name collisions/shadowing
    from config_parser import FeatureConfigParser, ProcessingContext
    from calculator import TechnicalCalculator
    from parquet_io import ParquetStorage
    from processor import FeatureProcessor

    config_parser = FeatureConfigParser(str(Path(CONFIG_DIR) / "features.json"))
    features = config_parser.parse()

    if not features:
        logger.info("ℹ️  No features defined in features.json. Skipping.")
        return

    settings = _load_settings()
    thread_count = settings.get("processing_threads", 4)

    ctx = ProcessingContext(
        thread_count=thread_count,
        data_dir=DATA_DIR,
        timeframes=["1D"],
        features=features,
    )

    storage = ParquetStorage(ctx.data_dir)
    raw_tickers = storage.get_available_tickers()

    if not raw_tickers:
        logger.info("ℹ️  No data available yet. Skipping feature calculation.")
        return

    # Pre-filter to exclude tickers missing the required parquet files
    valid_tickers = []
    base_dir = Path(ctx.data_dir)
    for t in raw_tickers:
        if all((base_dir / t / f"{tf}.parquet").exists() for tf in ctx.timeframes):
            valid_tickers.append(t)
            
    skipped = len(raw_tickers) - len(valid_tickers)
    if skipped > 0:
        logger.info("ℹ️  Skipped %d tickers without complete source files.", skipped)
        
    tickers = valid_tickers

    if not tickers:
        logger.info("ℹ️  No valid tickers with data found. Skipping.")
        return

    logger.info(
        "▶️  Calculating features for %d ticker(s) with %d thread(s)...",
        len(tickers),
        ctx.thread_count,
    )

    calculator = TechnicalCalculator()
    processor = FeatureProcessor(ctx, storage, calculator)
    results = processor.process_all_tickers(tickers, priority=priority, log_queue=log_queue)
    success_count = sum(1 for r in results if r.success)
    logger.info("✅ Feature calculation finished: %d/%d successful", success_count, len(results))

    # Calculate universe market breadth ($STATS.MARKET_BREADTH)
    try:
        from market_breadth import generate_market_breadth
        logger.info("📊 Generating universe market breadth ($STATS.MARKET_BREADTH)...")
        generate_market_breadth(ctx.data_dir)
    except Exception as e:
        logger.error("⚠️ Failed to generate market breadth: %s", e)



# ─── FastAPI App ─────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Stock Data Features API",
        description="Technical indicator calculation service.",
        version="1.0.0",
    )

    job_manager = JobManager()
    storage = ParquetStorage(DATA_DIR)

    @app.post("/features/calculate")
    async def trigger_feature_calculation(stream: bool = False, priority: str = None):
        """
        Triggers the feature calculation process.
        Returns 202 if started, 409 if already running. (F-API-010, F-SYS-030)
        If stream=True, returns a StreamingResponse with real-time logs.
        If priority is set, that ticker is processed first.
        """
        if stream:
            return StreamingResponse(
                job_manager.stream_feature_calculation(run_feature_pipeline, priority=priority),
                media_type="text/plain",
            )

        success = job_manager.start_feature_calculation(run_feature_pipeline, priority=priority)

        if success:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "Job started in background",
                    "priority": priority,
                    "hint": "Use ?stream=true to see real-time log output",
                },
            )
        else:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "status": "Ignored",
                    "detail": "A feature calculation process is already running.",
                },
            )

    from scheduler import FeatureScheduler
    scheduler = FeatureScheduler()

    @app.get("/status")
    @app.get("/features/status")
    async def get_status() -> dict:
        """Returns detailed live status and progress of feature calculation."""
        status_data = job_manager.get_detailed_status()
        status_data["schedule"] = scheduler.config
        return status_data

    @app.get("/features/schedule")
    async def get_schedule() -> dict:
        """Returns the current cyclical calculation schedule."""
        return scheduler.config

    @app.post("/features/schedule")
    async def set_schedule(cfg: dict) -> dict:
        """Updates and persists the cyclical calculation schedule in Supabase."""
        scheduler.save_config_to_db(cfg)
        return {"status": "ok", "schedule": scheduler.config}

    @app.get("/health")
    async def health_check() -> dict:
        """Simple liveness probe for Docker health checks."""
        return {"status": "ok"}

    return app


# ─── Entrypoint ──────────────────────────────────────────────────

def main() -> None:
    configure_logging(LOG_DIR)

    logger.info("═══════════════════════════════════════════════════════════════")
    logger.info("  Stock Data Features Service — starting up")
    logger.info("═══════════════════════════════════════════════════════════════")
    logger.info("ℹ️  Config dir: %s", CONFIG_DIR)
    logger.info("ℹ️  Data dir:   %s", DATA_DIR)
    logger.info("ℹ️  API port:   %d", API_PORT)

    from scheduler import FeatureScheduler
    scheduler = FeatureScheduler()
    scheduler.start(run_feature_pipeline)

    app = create_app()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
