"""
Cognetra 3.0 — Video Manager (Sequential Dataset Processing)
Discovers .mp4/.avi/.mov/.mkv/.webm files and facilitates sequential video processing.
Removes infinite looping logic to process finite dataset videos one by one.
"""
import logging
import os
from pathlib import Path
from typing import Generator, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("cognetra.video")

SUPPORTED_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


def discover_videos(videos_dir: str) -> List[str]:
    """Find all supported video files in the given directory (sorted)."""
    p = Path(videos_dir)
    if not p.exists():
        logger.warning(f"Videos directory not found: {videos_dir}")
        return []
    files = sorted([
        str(f) for f in p.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
    ])
    logger.info(f"Discovered {len(files)} video(s) in {videos_dir}")
    return files


def get_video_metadata(video_path: str) -> Tuple[float, int, float]:
    """
    Open video and extract: (fps, total_frames, total_duration_seconds).
    Cleanly releases the VideoCapture handle.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning(f"Cannot inspect video: {video_path}")
        return 25.0, 0, 0.0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / max(fps, 1.0)
    cap.release()
    return float(fps), int(total_frames), float(duration)


def stream_frames(
    videos_dir: str,
    loop: bool = False,
    frame_width: int = 320,
) -> Generator[Tuple[np.ndarray, float, str, int, int], None, None]:
    """
    Sequential frame generator.
    Iterates through discovered video files ONE BY ONE without infinite looping.
    Yields: (frame_bgr, fps, video_path, frame_index, total_frames)
    """
    videos = discover_videos(videos_dir)
    if not videos:
        logger.warning("No videos found in directory.")
        return

    for video_path in videos:
        logger.info(f"Opening video: {Path(video_path).name}")
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning(f"Cannot open: {video_path} — skipping")
            continue

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_idx = 0

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                yield frame, fps, video_path, frame_idx, total_frames
                frame_idx += 1
        finally:
            cap.release()
            logger.info(f"Finished: {Path(video_path).name} ({frame_idx} frames at {fps:.1f} fps)")

    logger.info("All videos in playlist processed sequentially.")
