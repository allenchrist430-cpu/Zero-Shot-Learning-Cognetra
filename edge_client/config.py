"""
Cognetra 3.0 — Edge Client Configuration
"""
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:9000",
    "camera_id": "CAM-001",
    "videos_dir": "videos",
    "queue_dir": "queue",
    "logs_dir": "logs",
    "loop_videos": False,
    "motion_threshold_multiplier": 2.2,
    "motion_area_threshold": 0.02,
    "cooldown_seconds": 15.0,
    "history_window_seconds": 1.5,
    "frame_width": 320,
    "upload_retry_max": 5,
    "upload_retry_base_delay": 2.0,
    "ttc_threshold_seconds": 1.5,
    "panic_brake_drop_pct": 0.65,
    "panic_brake_window_seconds": 0.5,
}

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """Load config from JSON file, falling back to defaults."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_CONFIG, **data}
            return merged
        except Exception as e:
            print(f"[Config] Warning: could not read config.json ({e}), using defaults")
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    """Persist config dict to JSON."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# Module-level config object
config = load_config()
