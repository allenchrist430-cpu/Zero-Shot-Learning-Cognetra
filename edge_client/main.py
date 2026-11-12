"""
Cognetra 3.0 — Edge Client Main Execution Loop
Sequential Dataset Processing & Dynamic Time-of-Crash Clipping
Pipeline: Frame-Diff / Kinetic Tracking → Dynamic Dual-Clip Slicing → Server Upload
"""
import logging
import os
import sys
import time
from pathlib import Path

import cv2

# Ensure edge_client dir is on path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from video_manager import discover_videos
from candidate_detector import CandidateDetector
from clip_manager import ClipManager
from upload_manager import UploadManager
from server_connection import check_server_health

logger = logging.getLogger("cognetra.client")


def setup_logging(logs_dir: str):
    """Configure logging to file + console."""
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(logs_dir) / "edge_client.log"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_path), mode="a", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def print_banner():
    banner = """
================================================================
          COGNETRA 3.0 — EDGE CLIENT NODE (IRIS)               
  Sequential Dataset Processing & Dynamic Time-of-Crash Clipping
  Pipeline: Frame Diff -> Kinetic Tracker -> Dual Clip -> Upload 
================================================================
"""
    print(banner)


def run():
    cfg = load_config()
    setup_logging(cfg["logs_dir"])
    print_banner()

    # Resolve paths relative to script directory
    base_dir = Path(__file__).parent
    videos_dir = str(base_dir / cfg["videos_dir"])
    queue_dir = str(base_dir / cfg["queue_dir"])
    server_url = cfg["server_url"]
    camera_id = cfg["camera_id"]

    logger.info(f"Server URL  : {server_url}")
    logger.info(f"Videos Dir  : {videos_dir}")
    logger.info(f"Camera ID   : {camera_id}")

    # Probe server health
    logger.info("Probing server health...")
    healthy, msg = check_server_health(server_url, timeout=10)
    if healthy:
        logger.info(f"[OK] Server reachable: {msg}")
    else:
        logger.warning(f"[WARN] Server not reachable: {msg}")
        logger.warning("Uploads will queue to disk until server becomes available.")

    # Discover videos sequentially
    videos = discover_videos(videos_dir)
    if not videos:
        logger.error(f"No video files found in {videos_dir}")
        logger.error("Please add .mp4/.avi/.mov/.mkv files to the videos/ directory.")
        sys.exit(1)

    logger.info(f"Discovered {len(videos)} video(s) for sequential dataset processing.")

    # Initialize detection, clip, and upload managers
    detector = CandidateDetector(cfg)
    clip_mgr = ClipManager(cfg, queue_dir)
    uploader = UploadManager(server_url, queue_dir, cfg)

    # Set of processed videos to guarantee no video is re-evaluated
    processed_videos = set()
    total_triggers = 0
    total_frames_processed = 0

    try:
        for idx, video_path in enumerate(videos, 1):
            if video_path in processed_videos:
                continue

            processed_videos.add(video_path)
            video_name = Path(video_path).name

            logger.info("\n" + "=" * 65)
            logger.info(f"[VIDEO {idx}/{len(videos)}] Opening: {video_name}")

            # Open video capture and calculate duration upon loading
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                logger.warning(f"Cannot open: {video_path} — skipping")
                continue

            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            total_duration = total_frames / max(fps, 1.0)

            logger.info(
                f"  Metadata: Total Duration = {total_duration:.2f}s | "
                f"FPS = {fps:.1f} | Total Frames = {total_frames}"
            )

            # Reset detector and buffer state for clean video isolation
            detector.reset()
            clip_mgr.reset_buffer()

            frame_idx = 0
            accident_detected = False

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    logger.info(f"Video {video_name} completed playback to end of file.")
                    break

                total_frames_processed += 1
                video_time = frame_idx / max(fps, 1.0)

                # Push to rolling buffer
                clip_mgr.push_frame(frame, frame_idx, video_time, fps)

                # Run candidate detector
                trigger = detector.process_frame(
                    frame=frame,
                    fps=fps,
                    video_path=video_path,
                    frame_index=frame_idx,
                    total_frames=total_frames,
                )

                if trigger:
                    total_triggers += 1
                    ev_id = trigger.event_id
                    accident_detected = True

                    logger.info(
                        f"\n{'=' * 65}\n"
                        f"[ACCIDENT TRIGGER #{total_triggers}] {trigger.trigger_type}\n"
                        f"  Event ID   : {ev_id}\n"
                        f"  Source     : {video_name}\n"
                        f"  Crash Time : T = {trigger.timestamp:.2f}s (of {total_duration:.2f}s total)\n"
                        f"  Confidence : {trigger.confidence:.2f}\n"
                        f"{'=' * 65}"
                    )

                    # 1. Dynamically slice Clip 1 (Impact: T-5s..T+5s) and Clip 2 (Aftermath: final 5s)
                    clip1_path, clip2_path = clip_mgr.cut_incident_clips(
                        event_id=ev_id,
                        trigger_type=trigger.trigger_type,
                        trigger_timestamp=trigger.timestamp,
                        trigger_frame_index=trigger.frame_index,
                        video_path=video_path,
                        fps=fps,
                        total_frames=total_frames,
                        total_duration=total_duration,
                        trigger_confidence=trigger.confidence,
                    )

                    # 2. Queue Clip 1 to upload manager
                    if clip1_path:
                        uploader.enqueue(
                            event_id=ev_id,
                            clip_number=1,
                            clip_path=clip1_path,
                            camera_id=camera_id,
                            source_video=video_name,
                            candidate_timestamp=trigger.timestamp,
                            edge_confidence=trigger.confidence,
                            speed=None,
                            vehicle_type=None,
                            event_type=trigger.trigger_type,
                        )

                    # 3. Queue Clip 2 to upload manager
                    if clip2_path:
                        uploader.enqueue(
                            event_id=ev_id,
                            clip_number=2,
                            clip_path=clip2_path,
                            camera_id=camera_id,
                            source_video=video_name,
                            candidate_timestamp=trigger.timestamp,
                            edge_confidence=trigger.confidence,
                            speed=None,
                            vehicle_type=None,
                            event_type=trigger.trigger_type,
                        )

                    logger.info(
                        f"[{ev_id}] Evidence clips queued to disk.\n"
                        f"Handoff: Stopping {video_name} and immediately transitioning to next video in playlist."
                    )

                    # Break out of frame loop immediately upon trigger
                    break

                frame_idx += 1

            # Cleanly close the video object
            cap.release()
            logger.info(f"Cleanly closed {video_name} (Accident detected: {accident_detected}).")

    except KeyboardInterrupt:
        logger.info("\nEdge client stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)
    finally:
        logger.info("\n" + "=" * 65)
        logger.info("Playlist processing complete.")
        logger.info(f"Total frames processed: {total_frames_processed:,}")
        logger.info(f"Total accident triggers: {total_triggers}")

        # Ensure pending background uploads finish before exit
        if uploader.pending_count() > 0:
            logger.info(f"Waiting for {uploader.pending_count()} pending upload(s) to finalize...")
            uploader.wait_for_idle(timeout=20.0)

        uploader.stop()
        logger.info("Edge Client shutdown cleanly.")


if __name__ == "__main__":
    run()
