"""
Cognetra 3.0 — Central Server Configuration
Loads from .env file in the central_server directory.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Explicitly load .env from the same directory as this file's parent (central_server/)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    load_dotenv(str(_env_file), override=True)

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Server
    HOST: str = "127.0.0.1"
    PORT: int = 9000
    DEBUG: bool = False

    # Paths (computed from __file__ location, not cwd)
    BASE_DIR: str = str(Path(__file__).parent.parent)
    DATA_DIR: str = str(Path(__file__).parent.parent / "data")
    CLIPS_DIR: str = str(Path(__file__).parent.parent / "clips")
    DASHBOARD_DIR: str = str(Path(__file__).parent.parent / "dashboard")

    # Database
    DATABASE_URL: str = ""  # filled in __init__

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash"
    GEMINI_TIMEOUT: int = 30

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALERT_CHAT_ID: str = ""

    # Verification thresholds
    EDGE_CONFIDENCE_BYPASS: float = 0.95
    MIN_GEMINI_CONFIDENCE: float = 0.60

    # SSE
    SSE_KEEPALIVE_INTERVAL: int = 15

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="allow",
    )

    def __init__(self, **data):
        super().__init__(**data)
        # Set DATABASE_URL dynamically if not overridden
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite+aiosqlite:///{self.DATA_DIR}/cognetra.db"


settings = Settings()

# Ensure directories exist
os.makedirs(settings.DATA_DIR, exist_ok=True)
os.makedirs(settings.CLIPS_DIR, exist_ok=True)
