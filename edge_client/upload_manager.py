"""
Cognetra 3.0 — Upload Manager
Disk-backed queue with exponential-retry multipart uploads to the central server.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("cognetra.upload")

QUEUE_VERSION = "1.0"


class UploadManager:
    """
    Manages a persistent disk queue of clip upload jobs.
    Each job is stored as a JSON file: queue/{event_id}_clip{n}.json
    A background daemon thread processes the queue with retry logic.
    """

    def __init__(self, server_url: str, queue_dir: str, cfg: dict):
        self._server_url = server_url.rstrip("/")
        self._queue_dir = Path(queue_dir)
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._cfg = cfg
        self._retry_max = cfg.get("upload_retry_max", 5)
        self._retry_base = cfg.get("upload_retry_base_delay", 2.0)
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Start background worker
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        logger.info(f"UploadManager started -> {self._server_url}")

    def enqueue(
        self,
        event_id: str,
        clip_number: int,
        clip_path: Optional[str],
        camera_id: str,
        source_video: str,
        candidate_timestamp: float,
        edge_confidence: float,
        speed: Optional[float],
        vehicle_type: Optional[str],
        event_type: str = "STANDARD",
    ):
        """Add a clip upload job to the disk queue."""
        if clip_path is None:
            logger.warning(f"[{event_id}] Clip {clip_number} path is None — skipping upload")
            return

        job_id = f"{event_id}_clip{clip_number}"
        job_path = self._queue_dir / f"{job_id}.json"

        job = {
            "version": QUEUE_VERSION,
            "job_id": job_id,
            "event_id": event_id,
            "clip_number": clip_number,
            "clip_path": clip_path,
            "camera_id": camera_id,
            "source_video": source_video,
            "candidate_timestamp": candidate_timestamp,
            "edge_confidence": edge_confidence,
            "speed": speed,
            "vehicle_type": vehicle_type,
            "event_type": event_type,
            "enqueued_at": time.time(),
            "attempts": 0,
        }

        with open(job_path, "w") as f:
            json.dump(job, f, indent=2)

        logger.info(f"[{event_id}] Enqueued Clip {clip_number} -> {job_path}")

    def _worker_loop(self):
        """Background thread: scan queue and attempt uploads."""
        while not self._stop_event.is_set():
            try:
                self._process_queue()
            except Exception as e:
                logger.error(f"Upload worker error: {e}")
            time.sleep(2.0)

    def _process_queue(self):
        """Read all pending queue files and attempt uploads."""
        job_files = sorted(self._queue_dir.glob("*.json"))
        for jf in job_files:
            job_id = jf.stem
            with self._lock:
                if job_id in self._inflight:
                    continue
                self._inflight.add(job_id)

            try:
                with open(jf, "r") as f:
                    job = json.load(f)

                success = self._upload_with_retry(job)

                if success:
                    jf.unlink(missing_ok=True)
                    logger.info(f"[{job['event_id']}] Clip {job['clip_number']} uploaded [OK] - queue entry removed")
                else:
                    # Update attempt count
                    job["attempts"] = job.get("attempts", 0) + 1
                    if job["attempts"] >= self._retry_max:
                        logger.error(
                            f"[{job['event_id']}] Clip {job['clip_number']} exceeded max retries "
                            f"({self._retry_max}) — removing from queue"
                        )
                        jf.unlink(missing_ok=True)
                    else:
                        with open(jf, "w") as f:
                            json.dump(job, f, indent=2)
            except Exception as e:
                logger.error(f"Queue processing error for {jf}: {e}")
            finally:
                with self._lock:
                    self._inflight.discard(job_id)

    def _upload_with_retry(self, job: dict) -> bool:
        """Attempt upload with exponential backoff. Returns True on HTTP 202."""
        clip_path = job["clip_path"]
        if not Path(clip_path).exists():
            logger.warning(f"[{job['event_id']}] Clip file missing: {clip_path}")
            return True  # Remove from queue — file gone

        for attempt in range(self._retry_max):
            try:
                with open(clip_path, "rb") as clip_file:
                    files = {"clip": (Path(clip_path).name, clip_file, "video/mp4")}
                    data = {
                        "event_id": job["event_id"],
                        "clip_number": str(job["clip_number"]),
                        "camera_id": job.get("camera_id", "unknown"),
                        "source_video": job.get("source_video", "unknown"),
                        "candidate_timestamp": str(job.get("candidate_timestamp", 0.0)),
                        "edge_confidence": str(job.get("edge_confidence", 0.0)),
                        "speed": str(job["speed"]) if job.get("speed") is not None else "",
                        "vehicle_type": job.get("vehicle_type", "") or "",
                        "event_type": job.get("event_type", "STANDARD"),
                    }

                    resp = requests.post(
                        f"{self._server_url}/api/upload_incident",
                        files=files,
                        data=data,
                        timeout=60,
                    )

                if resp.status_code == 202:
                    logger.info(
                        f"[{job['event_id']}] Clip {job['clip_number']} -> HTTP 202 "
                        f"(attempt {attempt + 1})"
                    )
                    return True
                else:
                    logger.warning(
                        f"[{job['event_id']}] Server returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )

            except requests.exceptions.ConnectionError:
                logger.warning(f"[{job['event_id']}] Server unreachable (attempt {attempt + 1})")
            except requests.exceptions.Timeout:
                logger.warning(f"[{job['event_id']}] Upload timeout (attempt {attempt + 1})")
            except Exception as e:
                logger.error(f"[{job['event_id']}] Upload error: {e}")

            if attempt < self._retry_max - 1:
                delay = self._retry_base * (2 ** attempt)
                logger.info(f"[{job['event_id']}] Retrying in {delay:.1f}s...")
                time.sleep(delay)

        return False

    def pending_count(self) -> int:
        """Return the number of un-uploaded jobs currently waiting in the disk queue."""
        return len(list(self._queue_dir.glob("*.json")))

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """Wait until all pending disk queue items have been uploaded."""
        start = time.time()
        while time.time() - start < timeout:
            if self.pending_count() == 0 and len(self._inflight) == 0:
                return True
            time.sleep(0.5)
        return self.pending_count() == 0

    def stop(self):
        """Signal the worker thread to stop."""
        self._stop_event.set()
