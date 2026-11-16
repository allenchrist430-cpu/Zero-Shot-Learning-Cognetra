"""
Cognetra 3.0 — Server-Side Algorithm Visualizer
Real YOLOv8n + ByteTrack tracking with LSTM-feature scoring streamed as MJPEG.

Endpoints:
  GET /api/analyze/{event_id}/{clip}  → MJPEG stream of annotated video
  GET /api/analyze/{event_id}/status  → JSON: processing status + LSTM scores
"""
import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional, Generator

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from config.settings import settings

logger = logging.getLogger("cognetra.analyzer")
router = APIRouter()

# ── Lazy-load YOLO (only when first request arrives) ─────────────────────────
_yolo_model = None
_yolo_lock   = threading.Lock()


def _get_model():
    """Thread-safe lazy load of YOLOv8n model (auto-downloads on first use)."""
    global _yolo_model
    if _yolo_model is None:
        with _yolo_lock:
            if _yolo_model is None:
                try:
                    from ultralytics import YOLO
                    logger.info("Loading YOLOv8n model (first request – may download ~6MB)…")
                    _yolo_model = YOLO("yolov8n.pt")
                    logger.info("YOLOv8n loaded ✓")
                except Exception as e:
                    logger.error(f"Failed to load YOLO: {e}")
                    _yolo_model = None
    return _yolo_model


# ── ByteTrack-compatible Tracker ─────────────────────────────────────────────
class SimpleByteTracker:
    """
    Lightweight ByteTrack-inspired tracker.
    Associates detections across frames using IoU + Kalman-lite prediction.
    Assigns persistent integer track IDs.
    """
    def __init__(self, iou_threshold: float = 0.35, max_lost: int = 20):
        self.next_id       = 1
        self.tracks: dict  = {}   # track_id -> {'box': xyxy, 'lost': int, 'label': str, 'trail': deque}
        self.iou_threshold = iou_threshold
        self.max_lost      = max_lost

    def _iou(self, a, b) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
        iw = max(0, ix2 - ix1); ih = max(0, iy2 - iy1)
        inter = iw * ih
        area_a = max(1, (ax2-ax1)*(ay2-ay1))
        area_b = max(1, (bx2-bx1)*(by2-by1))
        return inter / (area_a + area_b - inter + 1e-6)

    def update(self, detections: list) -> list:
        """
        detections: list of (x1,y1,x2,y2, label, conf)
        returns   : list of (x1,y1,x2,y2, track_id, label, conf)
        """
        # Mark all tracks as potentially lost
        for tid in list(self.tracks):
            self.tracks[tid]['lost'] += 1

        matched_det_ids = set()
        matched_track_ids = set()

        track_ids  = list(self.tracks.keys())
        track_boxes = [self.tracks[t]['box'] for t in track_ids]

        for d_idx, det in enumerate(detections):
            x1, y1, x2, y2, label, conf = det
            det_box = (x1, y1, x2, y2)
            best_iou = self.iou_threshold
            best_tid = None

            for t_idx, tid in enumerate(track_ids):
                if tid in matched_track_ids:
                    continue
                iou = self._iou(det_box, track_boxes[t_idx])
                if iou > best_iou:
                    best_iou = iou
                    best_tid = tid

            if best_tid is not None:
                # Update existing track
                trail = self.tracks[best_tid]['trail']
                cx = int((x1 + x2) / 2); cy = int((y1 + y2) / 2)
                trail.append((cx, cy))
                self.tracks[best_tid].update({
                    'box': det_box, 'lost': 0,
                    'label': label, 'conf': conf,
                    'trail': trail,
                })
                matched_det_ids.add(d_idx)
                matched_track_ids.add(best_tid)
            else:
                # New track
                cx = int((x1 + x2) / 2); cy = int((y1 + y2) / 2)
                self.tracks[self.next_id] = {
                    'box': det_box, 'lost': 0,
                    'label': label, 'conf': conf,
                    'trail': deque([(cx, cy)], maxlen=25),
                }
                matched_det_ids.add(d_idx)
                self.next_id += 1

        # Prune lost tracks
        for tid in list(self.tracks):
            if self.tracks[tid]['lost'] > self.max_lost:
                del self.tracks[tid]

        # Build result for active tracks
        result = []
        for tid, t in self.tracks.items():
            if t['lost'] == 0:
                x1, y1, x2, y2 = t['box']
                result.append((int(x1), int(y1), int(x2), int(y2), tid, t['label'], t['conf'], t['trail']))
        return result


# ── LSTM-feature Crash Scorer ─────────────────────────────────────────────────
class LSTMCrashScorer:
    """
    Mimics an LSTM with hand-crafted temporal features:
      - sudden velocity spike (kinetic burst)
      - box area explosion (crash deformation)
      - inter-track distance collapse (collision)
      - overlap between high-confidence boxes
    Hidden state is maintained across frames for smooth temporal scoring.
    """
    def __init__(self, n_classes: int = 5, decay: float = 0.75):
        self.decay    = decay
        self.h        = np.zeros(n_classes)   # LSTM hidden state (simulated)
        self.prev_boxes: dict = {}             # tid -> (cx, cy, area)
        self.frame_history = deque(maxlen=12)  # last 12 frame feature vectors

    def update(self, tracks: list) -> dict:
        """Accepts active tracks list, returns dict of scores."""
        if not tracks:
            self.h *= self.decay
            return self._scores_dict(self.h)

        # Feature extraction
        velocities   = []
        area_changes = []
        positions    = {}

        for x1, y1, x2, y2, tid, label, conf, trail in tracks:
            area = (x2 - x1) * (y2 - y1)
            cx   = (x1 + x2) / 2
            cy   = (y1 + y2) / 2
            positions[tid] = (cx, cy, area)

            if tid in self.prev_boxes:
                pcx, pcy, parea = self.prev_boxes[tid]
                dist = np.sqrt((cx - pcx)**2 + (cy - pcy)**2)
                velocities.append(dist)
                area_change = abs(area - parea) / max(parea, 1)
                area_changes.append(area_change)

        # Minimum distance between track centroids (collision proximity)
        min_dist = 1e9
        tids = list(positions.keys())
        for i in range(len(tids)):
            for j in range(i + 1, len(tids)):
                ax, ay, _ = positions[tids[i]]
                bx, by, _ = positions[tids[j]]
                d = np.sqrt((ax - bx)**2 + (ay - by)**2)
                min_dist = min(min_dist, d)
        if min_dist == 1e9:
            min_dist = 999

        # Overlap score
        overlap_score = 0.0
        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                ax1, ay1, ax2, ay2 = tracks[i][0], tracks[i][1], tracks[i][2], tracks[i][3]
                bx1, by1, bx2, by2 = tracks[j][0], tracks[j][1], tracks[j][2], tracks[j][3]
                ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
                ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
                if ix2 > ix1 and iy2 > iy1:
                    overlap_score += (ix2 - ix1) * (iy2 - iy1)

        # Normalise features to [0, 1]
        max_vel       = max(velocities)   / 60.0  if velocities   else 0.0
        max_area_chg  = max(area_changes) / 0.5   if area_changes else 0.0
        prox_score    = max(0.0, 1.0 - min_dist / 200.0)
        overlap_norm  = min(1.0, overlap_score / 5000.0)

        # Raw feature vector
        f = np.array([
            min(1.0, max_vel),          # crash    (velocity spike)
            min(1.0, max_area_chg),     # prop     (area deformation)
            min(1.0, max_vel * 1.3),    # motion-burst
            min(1.0, prox_score),       # fallen   (proximity)
            min(1.0, overlap_norm),     # evidence (overlap)
        ])

        self.frame_history.append(f)

        # LSTM-like hidden state update: h = decay*h + (1-decay)*f
        self.h = self.decay * self.h + (1 - self.decay) * f

        # Update previous boxes
        self.prev_boxes = {tid: positions[tid] for tid in positions}

        return self._scores_dict(self.h)

    @staticmethod
    def _scores_dict(h: np.ndarray) -> dict:
        return {
            'crash':        round(float(h[0]), 3),
            'prop':         round(float(h[1]), 3),
            'motion_burst': round(float(h[2]), 3),
            'fallen':       round(float(h[3]), 3),
            'evidence':     round(float(h[4]), 3),
        }


# ── Video Frame Annotator ──────────────────────────────────────────────────────
# COCO class IDs we care about: vehicle + person
VEHICLE_CLASSES = {2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck', 1: 'bicycle'}
PERSON_CLASSES  = {0: 'person'}
ALL_CLASSES     = {**VEHICLE_CLASSES, **PERSON_CLASSES}
# Classes to highlight differently (persons in blue, vehicles in yellow)
BOX_COLORS = {
    'car':        (0, 215, 255),   # golden yellow (BGR)
    'truck':      (0, 165, 255),   # orange
    'bus':        (0, 180, 255),   # amber
    'motorcycle': (0, 220, 255),   # yellow
    'bicycle':    (100, 220, 255), # light yellow
    'person':     (255, 100, 50),  # blue-ish
}

FONT   = cv2.FONT_HERSHEY_DUPLEX
FONT_S = 0.45
THICK  = 1


def _annotate_frame(
    frame: np.ndarray,
    tracks: list,
    scores: dict,
    lstm_val: float,
    frame_idx: int,
    fps: float,
) -> np.ndarray:
    """
    Draw all annotations on a single frame, matching the reference image:
    - Yellow bounding boxes with track IDs  (#214 car)
    - Coloured trails (motion path)
    - LSTM review badge (top-right)
    - Timestamp (top-left)
    - RED outer danger border if crash > 0.5
    - Bottom HUD bar: crash / prop / motion-burst / fallen / evidence
    - REC badge (top-right corner)
    """
    H, W = frame.shape[:2]
    overlay = frame.copy()

    # ── 1. Bounding boxes + labels + trails ──────────────────────────────────
    for x1, y1, x2, y2, tid, label, conf, trail in tracks:
        color = BOX_COLORS.get(label, (0, 215, 255))

        # Bounding box (thick golden border)
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)

        # Track ID label (black bg + colored text)
        tag = f"#{tid} {label}"
        (tw, th), _ = cv2.getTextSize(tag, FONT, FONT_S, THICK)
        cv2.rectangle(overlay, (x1, y1 - th - 6), (x1 + tw + 6, y1), (0, 0, 0), -1)
        cv2.putText(overlay, tag, (x1 + 3, y1 - 4), FONT, FONT_S, color, THICK, cv2.LINE_AA)

        # Confidence badge
        conf_txt = f"{conf:.2f}"
        cv2.putText(overlay, conf_txt, (x1 + 3, y2 - 5), FONT, 0.38, color, 1, cv2.LINE_AA)

        # Motion trail
        pts = list(trail)
        for i in range(1, len(pts)):
            alpha = i / len(pts)
            tc    = tuple(int(c * alpha) for c in color)
            cv2.line(overlay, pts[i - 1], pts[i], tc, 2, cv2.LINE_AA)

    # Blend overlay (semi-transparent boxes)
    frame = cv2.addWeighted(overlay, 0.85, frame, 0.15, 0)

    # ── 2. Top-left: filename / timestamp ────────────────────────────────────
    ts_txt = f"{time.strftime('%d/%m/%Y %H:%M:%S')}"
    cv2.rectangle(frame, (0, 0), (W, 24), (0, 0, 0), -1)
    cv2.putText(frame, ts_txt, (6, 15), FONT, 0.42, (180, 220, 255), 1, cv2.LINE_AA)

    # ── 3. Top-right: LSTM badge + REC ──────────────────────────────────────
    lstm_txt = f"LSTM review: ({lstm_val:.2f})"
    (lw, lh), _ = cv2.getTextSize(lstm_txt, FONT, 0.48, 1)
    lstm_x = W - lw - 14
    cv2.rectangle(frame, (lstm_x - 4, 2), (W - 4, 22), (0, 0, 0), -1)
    cv2.putText(frame, lstm_txt, (lstm_x, 16), FONT, 0.48, (0, 210, 255), 1, cv2.LINE_AA)

    # REC badge
    secs   = int(frame_idx / max(fps, 1))
    rec_mm = str(secs // 60).zfill(2)
    rec_ss = str(secs % 60).zfill(2)
    rec_txt = f"REC {rec_mm}:{rec_ss}"
    (rw, rh), _ = cv2.getTextSize(rec_txt, FONT, 0.44, 1)
    rx = W - rw - 10
    cv2.putText(frame, "●", (rx - 14, 42), FONT, 0.5, (0, 0, 230), 1, cv2.LINE_AA)
    cv2.putText(frame, rec_txt, (rx, 42), FONT, 0.44, (200, 200, 200), 1, cv2.LINE_AA)

    # ── 4. Danger border (red if crash > 0.5) ───────────────────────────────
    is_danger = scores['crash'] > 0.4
    if is_danger:
        brd = 5
        cv2.rectangle(frame, (0, 0), (W - 1, H - 1), (0, 0, 220), brd)
        # Pulsing "DANGER" text top-left
        danger_txt = "⚠ DANGER"
        cv2.putText(frame, danger_txt, (8, 55), FONT, 0.6, (0, 0, 230), 2, cv2.LINE_AA)

    # ── 5. Bottom HUD bar ────────────────────────────────────────────────────
    hud_h = 26
    cv2.rectangle(frame, (0, H - hud_h), (W, H), (0, 0, 0), -1)
    fallen_int = 1 if scores['fallen'] > 0.5 else 0
    hud_txt = (
        f"crash {scores['crash']:.2f}  "
        f"prop {scores['prop']:.2f}  "
        f"motion-burst {scores['motion_burst']:.2f}  "
        f"fallen {fallen_int}  "
        f"evidence {scores['evidence']:.2f}"
    )
    # Score bar colors: green→amber→red based on crash score
    crash_v = scores['crash']
    if crash_v > 0.5:
        hud_color = (60, 80, 230)   # red-ish
    elif crash_v > 0.25:
        hud_color = (30, 165, 255)  # amber
    else:
        hud_color = (80, 200, 80)   # green

    cv2.putText(frame, hud_txt, (8, H - 8), cv2.FONT_HERSHEY_PLAIN, 1.0, hud_color, 1, cv2.LINE_AA)

    # ── 6. YOLO + ByteTrack credit (bottom-right) ────────────────────────────
    credit = "YOLOv8n + ByteTrack | COGNETRA FORENSICS"
    (cw, _), _ = cv2.getTextSize(credit, FONT, 0.35, 1)
    cv2.putText(frame, credit, (W - cw - 6, H - 8), FONT, 0.35, (100, 100, 100), 1, cv2.LINE_AA)

    return frame


# ── MJPEG Stream Generator ────────────────────────────────────────────────────
def _stream_frames(video_path: str) -> Generator[bytes, None, None]:
    """
    Open clip, run YOLO+ByteTrack per frame, annotate, yield MJPEG chunks.
    Uses detect+track mode so YOLO itself feeds ByteTrack internally if available,
    otherwise falls back to our SimpleByteTracker.
    """
    model   = _get_model()
    cap     = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f"Cannot open video: {video_path}")
        return

    fps        = cap.get(cv2.CAP_PROP_FPS) or 25.0
    tracker    = SimpleByteTracker(iou_threshold=0.30, max_lost=15)
    scorer     = LSTMCrashScorer(decay=0.72)
    frame_idx  = 0
    scores     = {'crash': 0.0, 'prop': 0.0, 'motion_burst': 0.0, 'fallen': 0.0, 'evidence': 0.0}

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1

        detections = []

        if model is not None:
            try:
                # Use YOLO track() for built-in ByteTrack if ultralytics has tracker support
                results = model.track(
                    frame,
                    persist=True,
                    tracker="bytetrack.yaml",
                    classes=list(ALL_CLASSES.keys()),
                    conf=0.35,
                    iou=0.5,
                    verbose=False,
                )
                r = results[0]
                boxes = r.boxes
                if boxes is not None and boxes.xyxy is not None:
                    xyxy   = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, 'cpu') else boxes.xyxy
                    confs  = boxes.conf.cpu().numpy() if hasattr(boxes.conf, 'cpu') else boxes.conf
                    cls_ids= boxes.cls.cpu().numpy()  if hasattr(boxes.cls,  'cpu') else boxes.cls
                    tids   = boxes.id.cpu().numpy()   if (boxes.id is not None and hasattr(boxes.id, 'cpu')) else (boxes.id if boxes.id is not None else np.arange(len(xyxy)))

                    active_tracks = []
                    for i, (box, cid, conf) in enumerate(zip(xyxy, cls_ids, confs)):
                        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                        label = ALL_CLASSES.get(int(cid), 'vehicle')
                        tid   = int(tids[i]) if tids is not None and i < len(tids) else (i + 1)
                        color = BOX_COLORS.get(label, (0, 215, 255))
                        trail = deque([(int((x1+x2)/2), int((y1+y2)/2))], maxlen=25)
                        active_tracks.append((x1, y1, x2, y2, tid, label, float(conf), trail))

                    scores = scorer.update([
                        (x1, y1, x2, y2, label, conf, None, None)[:6]
                        for x1, y1, x2, y2, tid, label, conf, trail in active_tracks
                    ])

                    lstm_val = scores['crash'] * 0.6 + scores['motion_burst'] * 0.4
                    frame    = _annotate_frame(frame, active_tracks, scores, lstm_val, frame_idx, fps)

            except Exception as e:
                # Fallback to our custom ByteTracker
                logger.debug(f"YOLO track fallback: {e}")
                try:
                    results  = model.predict(frame, classes=list(ALL_CLASSES.keys()), conf=0.35, verbose=False)
                    r        = results[0]
                    boxes    = r.boxes
                    if boxes is not None and boxes.xyxy is not None:
                        xyxy   = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, 'cpu') else boxes.xyxy
                        confs  = boxes.conf.cpu().numpy() if hasattr(boxes.conf, 'cpu') else boxes.conf
                        cls_ids= boxes.cls.cpu().numpy()  if hasattr(boxes.cls,  'cpu') else boxes.cls
                        for box, cid, conf in zip(xyxy, cls_ids, confs):
                            x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                            label = ALL_CLASSES.get(int(cid), 'vehicle')
                            detections.append((x1, y1, x2, y2, label, float(conf)))

                        active_tracks = tracker.update(detections)
                        scored_input  = [(x1, y1, x2, y2, lbl, conf) for x1, y1, x2, y2, tid, lbl, conf, trail in active_tracks]
                        scores        = scorer.update(scored_input)
                        lstm_val      = scores['crash'] * 0.6 + scores['motion_burst'] * 0.4
                        frame         = _annotate_frame(frame, active_tracks, scores, lstm_val, frame_idx, fps)
                except Exception as e2:
                    logger.warning(f"YOLO predict also failed: {e2}")
        else:
            # No model — write "YOLO unavailable" message on frame
            cv2.putText(frame, "YOLOv8 not loaded", (10, 30), FONT, 0.7, (0, 0, 200), 2)

        # Encode frame as JPEG
        ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            yield (
                b'--cognetraframe\r\n'
                b'Content-Type: image/jpeg\r\n\r\n'
                + buf.tobytes()
                + b'\r\n'
            )

    cap.release()


# ── API Endpoints ─────────────────────────────────────────────────────────────
@router.get("/analyze/list")
async def list_available_clips():
    """List all available event clips on disk for analysis."""
    clips_dir = Path(settings.CLIPS_DIR)
    events = []
    if clips_dir.exists():
        for d in sorted(clips_dir.glob("EVT-*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir():
                c1 = (d / "clip1.mp4").exists()
                c2 = (d / "clip2.mp4").exists()
                if c1 or c2:
                    events.append({
                        "event_id": d.name,
                        "clip1": c1,
                        "clip2": c2,
                        "mtime": d.stat().st_mtime,
                    })
    return {"events": events, "count": len(events)}


@router.get("/analyze/{event_id}/status")
async def analyze_status(event_id: str):
    """Check YOLO model availability and clip status for an event."""
    model_ready = _get_model() is not None
    clips_dir   = Path(settings.CLIPS_DIR) / event_id
    return {
        "event_id":    event_id,
        "model_ready": model_ready,
        "clip1_ready": (clips_dir / "clip1.mp4").exists(),
        "clip2_ready": (clips_dir / "clip2.mp4").exists(),
    }


@router.get("/analyze/{event_id}/{clip}")
async def stream_analyzed_clip(event_id: str, clip: str):
    """
    MJPEG stream of a clip annotated with YOLOv8n + ByteTrack + LSTM scoring.
    clip: 'clip1' | 'clip2' | 'source'
    """
    clips_dir = Path(settings.CLIPS_DIR)
    
    if clip in ("clip1", "clip2"):
        video_path = clips_dir / event_id / f"{clip}.mp4"
    elif clip == "source":
        # Look for source video in edge_client/videos
        source_dir = Path(settings.BASE_DIR).parent / "edge_client" / "videos"
        # Find by event ID prefix matching (best effort)
        video_path = None
        if source_dir.exists():
            for f in source_dir.glob("*.mp4"):
                video_path = f
                break  # use first available as demo
        if video_path is None:
            raise HTTPException(status_code=404, detail="Source video not found")
    else:
        raise HTTPException(status_code=400, detail="clip must be clip1, clip2, or source")

    if clip != "source" and not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Clip not found: {video_path}")

    return StreamingResponse(
        _stream_frames(str(video_path)),
        media_type="multipart/x-mixed-replace; boundary=cognetraframe",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
    )
