"""
Cognetra 3.0 - FastAPI Central Server Entry Point
"""
import logging
import os
import sys
from pathlib import Path

# Ensure the server directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent))

# Load .env BEFORE importing settings (critical for API key pickup)
from dotenv import load_dotenv
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(str(_env_path), override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from models.database import init_db
from api.upload import router as upload_router
from api.events import router as events_router
from api.dashboard import router as dashboard_router
from api.analyzer import router as analyzer_router

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cognetra.server")

# ── App Factory ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Cognetra 3.0 - Intelligent Accident Detection Platform",
    description=(
        "Zero-Shot Learning-Based Intelligent Multi-Domain Object Recognition "
        "and Decision Support Platform for Dynamic Real-World Environments"
    ),
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    logger.info("=" * 60)
    logger.info("  COGNETRA 3.0 - Central Server Starting")
    logger.info(f"  Listening : http://{settings.HOST}:{settings.PORT}")
    logger.info(f"  Backend   : http://{settings.HOST}:{settings.PORT}/")
    logger.info(f"  Monitor   : http://{settings.HOST}:{settings.PORT}/monitor")
    logger.info(f"  Gemini Key: {'SET (' + settings.GEMINI_API_KEY[:8] + '...)' if settings.GEMINI_API_KEY else 'NOT SET'}")
    logger.info(f"  Telegram  : {'SET' if settings.TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    logger.info("=" * 60)
    await init_db()
    logger.info("Database initialized OK")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Cognetra server shutting down...")


# ── Health Endpoint ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "server": "cognetra",
        "version": "3.0.0",
        "host": settings.HOST,
        "port": settings.PORT,
    }


# ── API Routers ───────────────────────────────────────────────────────────────
app.include_router(upload_router, prefix="/api", tags=["Upload"])
app.include_router(events_router, prefix="/api", tags=["SSE"])
app.include_router(dashboard_router, prefix="/api", tags=["Dashboard"])
app.include_router(analyzer_router, prefix="/api", tags=["Analyzer"])

# ── Static Dashboard ──────────────────────────────────────────────────────────
dashboard_path = Path(settings.DASHBOARD_DIR)

if dashboard_path.exists():
    app.mount("/static", StaticFiles(directory=str(dashboard_path)), name="static")

    @app.get("/", response_class=FileResponse)
    async def serve_backend_dashboard():
        return FileResponse(
            str(dashboard_path / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
        )

    @app.get("/monitor", response_class=FileResponse)
    async def serve_monitor_dashboard():
        return FileResponse(
            str(dashboard_path / "monitor.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
        )

    @app.get("/analyzer", response_class=FileResponse)
    async def serve_analyzer_dashboard():
        return FileResponse(
            str(dashboard_path / "analyzer.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
        )

else:
    @app.get("/")
    async def dashboard_placeholder():
        return JSONResponse({"message": "Dashboard not found. Place index.html in /dashboard/"})

    @app.get("/monitor")
    async def monitor_placeholder():
        return JSONResponse({"message": "Monitor not found. Place monitor.html in /dashboard/"})

    @app.get("/analyzer")
    async def analyzer_placeholder():
        return JSONResponse({"message": "Analyzer not found. Place analyzer.html in /dashboard/"})


# ── Clips & Source Videos serving ──────────────────────────────────────────────
clips_path = Path(settings.CLIPS_DIR)
if clips_path.exists():
    app.mount("/clips", StaticFiles(directory=str(clips_path)), name="clips")

source_videos_path = Path(settings.BASE_DIR).parent / "edge_client" / "videos"
if source_videos_path.exists():
    app.mount("/source_videos", StaticFiles(directory=str(source_videos_path)), name="source_videos")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info",
    )
