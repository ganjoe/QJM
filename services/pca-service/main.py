import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chart_data import router as chart_router
from indicators import router as indicator_router
from scanners import router as scanner_router
from watchlists_api import router as watchlist_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger("pca.main")

app = FastAPI(
    title="QJM PCA Service",
    description="Pattern & Chart Analysis Service: Parquet OHLCV reader, technical indicators, scanners, and watchlists.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount sub-routers under /api
app.include_router(chart_router, prefix="/api")
app.include_router(indicator_router, prefix="/api")
app.include_router(scanner_router, prefix="/api")
app.include_router(watchlist_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pca-service"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8791, reload=True)
