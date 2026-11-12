"""
Cognetra 3.0 — Candidate Detector (v2 — Precision Multi-Criteria Engine)
Frame-differencing motion anomaly detector with ByteTrack-inspired velocity tracking.

Detection Priority (in order):
  1. MOTION_TRIGGER  — Physical bounding-box overlap between tracked blobs (direct impact)
  2. MOTION_TRIGGER  — Adaptive kinetic energy spike (three-tier thresholding)
  3. PREDICTIVE_TRIGGER — Geometric TTC ≤ 1.5s via CPA trajectory projection
  4. NEAR_MISS_TRIGGER  — Panic braking: >75% speed drop in <0.45s to near-stop

Key fixes vs v1:
  - _in_cooldown / _reset_cooldown now use video_time (not wall-clock time.monotonic())
  - TrackedObject.update() receives video_time for timestamp_history (not monotonic)
  - 2.2s warmup window suppresses initial frame stabilization false positives
  - _calculate_ttc uses geometric CPA (closest-point-of-approach) with d_min check
  - _check_panic_braking requires peak ≥ 40 px/s and final speed ≤ 12 px/s (real lock-up)
  - Morph CLOSE kernel merges crash debris into coherent blobs for overlap detection
  - Adaptive energy threshold: major / medium / distant collision tiers
"""
import collections
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, List, Dict

import cv2
import numpy as np

logger = logging.getLogger("cognetra.detector")

EVENT_ID_REGEX = re.compile(r"^[A-Za-z0-9_\-]+$")


def generate_event_id(prefix: str = "EVT") -> str:
    """Generate a unique event ID: EVT-YYYYMMDD-HHMMSS-XXXXXX"""
    import random
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"{random.randint(0, 999999):06d}"
    eid = f"{prefix}-{ts}-{suffix}"
    assert EVENT_ID_REGEX.match(eid) and len(eid) <= 64, f"Invalid event_id: {eid}"
    return eid


def _compute_iou(boxA: Tuple[int, int, int, int], boxB: Tuple[int, int, int, int]) -> Tuple[float, float]:
    """
    Returns (iou, min_overlap_fraction) for two bounding boxes.
    min_overlap = intersection / smaller box area — detects when a large truck
    fully overlaps a smaller car's bounding box.
    """
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    interW = max(0, xB - xA)
    interH = max(0, yB - yA)
    interArea = interW * interH

    boxAArea = max(1, (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    boxBArea = max(1, (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))

    iou = interArea / float(boxAArea + boxBArea - interArea + 1e-6)
    overlap_min = interArea / float(min(boxAArea, boxBArea) + 1e-6)
    return iou, overlap_min


@dataclass
class TrackedObject:
    """Lightweight ByteTrack-inspired tracker state for a detected moving region."""
    track_id: int
    bbox: Tuple[int, int, int, int]  # x1, y1, x2, y2
    centroid: Tuple[float, float]
    speed_history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=30))
    timestamp_history: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=30)
    )
    velocity: Tuple[float, float] = (0.0, 0.0)  # px/s in x, y
    last_speed: float = 0.0
    age: int = 1  # frames alive

    def update(self, new_bbox: Tuple[int, int, int, int], fps: float, video_time: float = 0.0):
        """Update tracker state with new bounding box and VIDEO timestamp (not wall-clock)."""
        x1, y1, x2, y2 = new_bbox
        new_cx = (x1 + x2) / 2.0
        new_cy = (y1 + y2) / 2.0
        old_cx, old_cy = self.centroid

        dt = 1.0 / max(fps, 1.0)
        vx = (new_cx - old_cx) / dt
        vy = (new_cy - old_cy) / dt
        speed = (vx ** 2 + vy ** 2) ** 0.5

        self.velocity = (vx, vy)
        self.last_speed = speed
        self.speed_history.append(speed)
        self.timestamp_history.append(video_time)   # ← video_time, NOT time.monotonic()
        self.bbox = new_bbox
        self.centroid = (new_cx, new_cy)
        self.age += 1


@dataclass
class TriggerEvent:
    event_id: str
    trigger_type: str  # MOTION_TRIGGER | PREDICTIVE_TRIGGER | NEAR_MISS_TRIGGER
    timestamp: float   # seconds into the video
    frame_index: int
    confidence: float
    video_path: str
    fps: float


class CandidateDetector:
    """
    Detects crash candidates using frame differencing + lightweight tracker.
    Three trigger types are emitted:
      MOTION_TRIGGER     — large spike in pixel diff OR physical bbox overlap (classic impact)
      PREDICTIVE_TRIGGER — geometric TTC projection ≤ 1.5s with CPA miss-distance check
      NEAR_MISS_TRIGGER  — severe panic braking detected (peak≥40 px/s → near-stop)
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._prev_gray: Optional[np.ndarray] = None
        self._motion_history: collections.deque = collections.deque(maxlen=200)
        self._last_trigger_time: float = -999.0   # video time, NOT wall-clock
        self._frame_width: int = cfg.get("frame_width", 320)
        self._cooldown: float = cfg.get("cooldown_seconds", 15.0)
        self._ttc_thresh: float = cfg.get("ttc_threshold_seconds", 1.5)
        self._panic_drop: float = cfg.get("panic_brake_drop_pct", 0.75)
        self._panic_window: float = cfg.get("panic_brake_window_seconds", 0.45)
        self._history_window: float = cfg.get("history_window_seconds", 2.0)
        self._warmup_seconds: float = cfg.get("warmup_seconds", 2.2)
        self._tracks: Dict[int, TrackedObject] = {}
        self._next_track_id: int = 0
        self._morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))

    # ── Cooldown (video-time based) ────────────────────────────────────────

    def _in_cooldown(self, video_time: float) -> bool:
        """Uses video time to prevent duplicate triggers within cooldown window."""
        return (video_time - self._last_trigger_time) < self._cooldown

    def _reset_cooldown(self, video_time: float):
        self._last_trigger_time = video_time

    # ── Frame differencing ─────────────────────────────────────────────────

    def _compute_motion_energy(self, frame: np.ndarray) -> Tuple[float, np.ndarray]:
        """
        Compute motion energy via frame differencing + morphological close.
        Returns (energy_fraction, closed_binary_mask).
        """
        h, w = frame.shape[:2]
        new_w = self._frame_width
        new_h = int(h * new_w / w)
        small = cv2.resize(frame, (new_w, new_h))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (9, 9), 0)

        if self._prev_gray is None or self._prev_gray.shape != blurred.shape:
            self._prev_gray = blurred
            return 0.0, np.zeros_like(blurred)

        diff = cv2.absdiff(self._prev_gray, blurred)
        self._prev_gray = blurred
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, self._morph_kernel)
        active_pixels = np.count_nonzero(thresh)
        total_pixels = thresh.size
        energy = active_pixels / total_pixels
        return energy, closed

    # ── Lightweight region tracker (ByteTrack-inspired) ───────────────────

    def _update_tracks(self, diff_mask: np.ndarray, frame: np.ndarray, fps: float, video_time: float):
        """Find contour blobs and update/create tracks. Passes video_time to update()."""
        contours, _ = cv2.findContours(diff_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Scale contours back to original frame coords
        h_orig, w_orig = frame.shape[:2]
        h_small, w_small = diff_mask.shape
        sx = w_orig / w_small
        sy = h_orig / h_small

        new_bboxes: List[Tuple[int, int, int, int]] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 40:  # filter tiny noise
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            new_bboxes.append((int(x * sx), int(y * sy), int((x + w) * sx), int((y + h) * sy)))

        # Simple nearest-centroid matching
        matched_ids = set()
        for bbox in new_bboxes:
            cx = (bbox[0] + bbox[2]) / 2.0
            cy = (bbox[1] + bbox[3]) / 2.0
            best_id = None
            best_dist = float("inf")
            for tid, track in self._tracks.items():
                if tid in matched_ids:
                    continue
                dx = cx - track.centroid[0]
                dy = cy - track.centroid[1]
                dist = (dx ** 2 + dy ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_id = tid

            if best_id is not None and best_dist < 100:
                self._tracks[best_id].update(bbox, fps, video_time)
                matched_ids.add(best_id)
            else:
                # New track
                tid = self._next_track_id
                self._next_track_id += 1
                cx2 = (bbox[0] + bbox[2]) / 2.0
                cy2 = (bbox[1] + bbox[3]) / 2.0
                new_track = TrackedObject(track_id=tid, bbox=bbox, centroid=(cx2, cy2))
                new_track.speed_history.append(0.0)
                new_track.timestamp_history.append(video_time)
                self._tracks[tid] = new_track

        # Prune stale tracks
        stale = [tid for tid in self._tracks if tid not in matched_ids]
        for tid in stale:
            del self._tracks[tid]

    # ── Priority 1: Bounding-box physical overlap ─────────────────────────

    def _check_overlap_collision(self) -> Optional[Tuple[int, int, float]]:
        """
        Checks if two established tracks physically overlap — the clearest indicator
        of a direct vehicular collision. Requires track age ≥ 4 frames.
        Returns (track_id_a, track_id_b, overlap_score) or None.
        """
        track_list = [t for t in self._tracks.values() if t.age >= 4]
        for i in range(len(track_list)):
            for j in range(i + 1, len(track_list)):
                iou, min_ov = _compute_iou(track_list[i].bbox, track_list[j].bbox)
                if iou >= 0.10 or min_ov >= 0.20:
                    return track_list[i].track_id, track_list[j].track_id, max(iou, min_ov)
        return None

    # ── Priority 3: TTC calculation (geometric CPA) ───────────────────────

    def _calculate_ttc(self, track_a: TrackedObject, track_b: TrackedObject) -> Optional[float]:
        """
        Calculates Time-to-Collision using closest-point-of-approach (CPA) geometry.
        Only fires when trajectories are on a genuine collision course (d_min ≤ threshold).
        Requires track age ≥ 6 and relative speed ≥ 7 px/s.
        Returns TTC in seconds, or None if no collision course detected.
        """
        if track_a.age < 6 or track_b.age < 6:
            return None

        ax, ay = track_a.centroid
        bx, by = track_b.centroid
        vax, vay = track_a.velocity
        vbx, vby = track_b.velocity

        rx = ax - bx
        ry = ay - by
        rvx = vax - vbx
        rvy = vay - vby

        v_rel_sq = rvx ** 2 + rvy ** 2
        if v_rel_sq < 49.0:  # relative speed < 7 px/s — both stationary or parallel
            return None

        # Closing rate: dot(-r, v_rel) must be positive (approaching)
        closing = -(rx * rvx + ry * rvy)
        if closing <= 0:
            return None  # moving apart

        # Time to CPA
        t_cpa = closing / (v_rel_sq + 1e-6)
        if t_cpa <= 0.2 or t_cpa > self._ttc_thresh:
            return None

        # Separation at CPA
        cpa_x = rx + rvx * t_cpa
        cpa_y = ry + rvy * t_cpa
        d_min = (cpa_x ** 2 + cpa_y ** 2) ** 0.5

        # Collision radius: half-width of combined bounding boxes
        wa = track_a.bbox[2] - track_a.bbox[0]
        wb = track_b.bbox[2] - track_b.bbox[0]
        collision_thresh = max(35.0, (wa + wb) * 0.35)

        return t_cpa if d_min <= collision_thresh else None

    # ── Priority 4: Panic braking detection ──────────────────────────────

    def _check_panic_braking(self, track: TrackedObject) -> bool:
        """
        Detects genuine ABS/panic braking near-miss:
        - Track must be alive ≥ 7 frames (avoid fresh-spawn noise)
        - Peak speed ≥ 40 px/s (was actually moving at speed)
        - Final speed ≤ 12 px/s (nearly stopped)
        - Drop ≥ 75% within the panic window
        Uses video-time timestamps (not wall-clock).
        """
        if track.age < 7:
            return False
        speeds = list(track.speed_history)
        times = list(track.timestamp_history)
        if len(speeds) < 5:
            return False

        now = times[-1]
        window_speeds = [
            s for s, t in zip(speeds, times)
            if now - t <= self._panic_window
        ]
        if len(window_speeds) < 3:
            return False

        peak = max(window_speeds)
        current = window_speeds[-1]

        if peak < 40.0 or current > 12.0:
            return False

        drop_pct = (peak - current) / (peak + 1e-6)
        return drop_pct >= self._panic_drop

    # ── Main processing ───────────────────────────────────────────────────

    def process_frame(
        self,
        frame: np.ndarray,
        fps: float,
        video_path: str,
        frame_index: int,
        total_frames: int,
    ) -> Optional[TriggerEvent]:
        """
        Process one frame. Returns a TriggerEvent if a trigger fires, else None.
        Detection order: Overlap → Energy Spike → TTC → Panic Braking
        """
        energy, closed_mask = self._compute_motion_energy(frame)
        video_time = frame_index / max(fps, 1.0)

        # Update motion history
        self._motion_history.append(energy)

        # Compute dynamic baseline from history window
        history_frames = int(self._history_window * fps)
        recent = list(self._motion_history)[-max(1, history_frames):]
        baseline = sum(recent) / len(recent) if recent else 0.001

        # Update tracks for predictive and overlap detection
        self._update_tracks(closed_mask, frame, fps, video_time)

        # ── Warmup: suppress triggers during video stabilisation ──────────
        if video_time < self._warmup_seconds:
            return None

        if self._in_cooldown(video_time):
            return None

        # ── Priority 1: Physical bounding-box overlap (direct impact) ─────
        overlap = self._check_overlap_collision()
        if overlap is not None:
            t1, t2, ov_val = overlap
            logger.warning(
                f"[MOTION_TRIGGER/OVERLAP] Physical collision between tracks {t1} & {t2} "
                f"(overlap={ov_val:.2f}) @ T={video_time:.2f}s"
            )
            self._reset_cooldown(video_time)
            return TriggerEvent(
                event_id=generate_event_id("EVT"),
                trigger_type="MOTION_TRIGGER",
                timestamp=video_time,
                frame_index=frame_index,
                confidence=min(0.99, 0.75 + ov_val * 0.25),
                video_path=video_path,
                fps=fps,
            )

        # ── Priority 2: Adaptive kinetic energy spike ─────────────────────
        # Three tiers to handle different zoom levels and vehicle sizes:
        #   Tier A: Major close-range collision  (e ≥ 0.035)
        #   Tier B: Medium intersection crash    (0.019 ≤ e and e ≥ baseline × 1.8)
        #   Tier C: Distant / zoomed-out crash   (0.010 ≤ e and e ≥ baseline × 2.8)
        is_energy_trigger = False
        if energy >= 0.035:
            is_energy_trigger = True
        elif energy >= 0.019 and energy >= baseline * 1.8:
            is_energy_trigger = True
        elif energy >= 0.010 and energy >= baseline * 2.8:
            is_energy_trigger = True

        if is_energy_trigger:
            conf = min(0.99, energy / (baseline * 2.0 + 1e-6) * 0.5 + 0.3)
            logger.info(
                f"[MOTION_TRIGGER] energy={energy:.4f} baseline={baseline:.4f} "
                f"conf={conf:.2f} @ T={video_time:.2f}s frame {frame_index}"
            )
            self._reset_cooldown(video_time)
            return TriggerEvent(
                event_id=generate_event_id("EVT"),
                trigger_type="MOTION_TRIGGER",
                timestamp=video_time,
                frame_index=frame_index,
                confidence=conf,
                video_path=video_path,
                fps=fps,
            )

        # ── Priority 3: Geometric TTC collision course ────────────────────
        track_list = list(self._tracks.values())
        for i in range(len(track_list)):
            for j in range(i + 1, len(track_list)):
                ttc = self._calculate_ttc(track_list[i], track_list[j])
                if ttc is not None:
                    logger.warning(
                        f"[PREDICTIVE_TRIGGER] TTC={ttc:.2f}s between tracks "
                        f"{track_list[i].track_id} & {track_list[j].track_id}"
                    )
                    self._reset_cooldown(video_time)
                    return TriggerEvent(
                        event_id=generate_event_id("EVT"),
                        trigger_type="PREDICTIVE_TRIGGER",
                        timestamp=video_time,
                        frame_index=frame_index,
                        confidence=min(0.85, (self._ttc_thresh - ttc) / self._ttc_thresh + 0.5),
                        video_path=video_path,
                        fps=fps,
                    )

        # ── Priority 4: Panic braking near-miss ──────────────────────────
        for track in track_list:
            if self._check_panic_braking(track):
                logger.warning(
                    f"[NEAR_MISS_TRIGGER] Panic braking detected on track {track.track_id} "
                    f"@ T={video_time:.2f}s"
                )
                self._reset_cooldown(video_time)
                return TriggerEvent(
                    event_id=generate_event_id("EVT"),
                    trigger_type="NEAR_MISS_TRIGGER",
                    timestamp=video_time,
                    frame_index=frame_index,
                    confidence=0.70,
                    video_path=video_path,
                    fps=fps,
                )

        return None

    def reset(self):
        """Reset detector state between videos."""
        self._prev_gray = None
        self._motion_history.clear()
        self._tracks.clear()
        self._last_trigger_time = -999.0
        self._next_track_id = 0
