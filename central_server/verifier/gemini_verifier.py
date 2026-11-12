"""
Cognetra 3.0 — Google Gemini VLM Verification Engine
Uses the new google-genai SDK (google.genai).
Fail-open: if Gemini API times out/fails, dispatch proceeds anyway.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from config.settings import settings

logger = logging.getLogger("cognetra.gemini")


@dataclass
class IncidentReport:
    """Represents a full incident for Gemini verification."""
    event_id: str
    clip1_path: Optional[str] = None
    clip2_path: Optional[str] = None
    edge_confidence: float = 0.0
    candidate_timestamp: float = 0.0
    camera_id: str = "unknown"
    source_video: str = "unknown"
    speed: Optional[float] = None
    vehicle_type: Optional[str] = None
    event_type: str = "STANDARD"       # STANDARD | PREDICTIVE_TRIGGER | NEAR_MISS_TRIGGER
    is_near_miss: bool = False         # Set by edge client for near-miss events


@dataclass
class VerificationResult:
    """Result returned by the Gemini verifier."""
    event_id: str
    is_accident: bool = False
    is_near_miss: bool = False
    confidence: float = 0.0
    severity: str = "UNKNOWN"
    collision_type: str = "UNKNOWN"
    vehicles_involved: str = "Unknown"
    heavy_vehicle_involved: bool = False
    entrapment_risk: bool = False
    rationale: str = ""
    road_condition: str = "UNKNOWN"
    obstruction_detected: bool = False
    raw_response: str = ""
    fail_open: bool = False
    verification_time_ms: float = 0.0


def _extract_jpeg_frames(video_path: str, max_frames: int = 6) -> list[bytes]:
    """Extract key frames from a video clip as raw JPEG bytes for Gemini."""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        step = max(1, total_frames // max_frames)

        frames_data: list[bytes] = []
        for i in range(0, min(total_frames, max_frames * step), step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                break
            # Resize frame width to max 640 for rapid transmission and optimal VLM recognition
            h, w = frame.shape[:2]
            new_w = min(w, 640)
            new_h = int(h * new_w / w)
            small = cv2.resize(frame, (new_w, new_h))
            _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            frames_data.append(buf.tobytes())
        cap.release()
        return frames_data
    except Exception as e:
        logger.warning(f"Frame extraction error: {e}")
        return []


def _build_prompt(report: IncidentReport) -> str:
    metadata_lines = [
        f"EVENT METADATA:",
        f"- Event ID: {report.event_id}",
        f"- Camera ID: {report.camera_id}",
        f"- Edge AI Confidence: {report.edge_confidence:.2f}",
        f"- Edge Trigger Type: {report.event_type}",
    ]
    if report.vehicle_type:
        metadata_lines.append(f"- Edge Detected Vehicle: {report.vehicle_type}")
    if report.speed:
        metadata_lines.append(f"- Estimated Approach Speed: {report.speed:.1f} km/h")
    meta_str = "\n".join(metadata_lines)

    return (
        "You are an expert Forensic Computer Vision and Traffic Incident Analysis AI for an emergency dispatch platform.\n"
        f"Carefully examine the surveillance video frames (Clip 1 = Impact Window, Clip 2 = Aftermath Window).\n\n"
        f"{meta_str}\n\n"
        "PHYSICS-INFORMED COLLISION & VEHICLE DYNAMICS MANDATE:\n"
        "1. VEHICLE IDENTIFICATION & MASS ASSESSMENT:\n"
        "   - Identify all vehicle classes involved: Heavy Commercial Trucks (semi-trailers, 18-wheelers, tractor-trailers, dump trucks, tankers), Buses, Delivery Vans, Passenger Cars/SUVs, Two-Wheelers (motorcycles/scooters), or Pedestrians.\n"
        "   - Note: Large commercial trucks have massive kinetic momentum (p = m*v) and large geometric footprints. When heavy trucks collide, roll over, jackknife, broadside, or crush other vehicles or road structures, the incident is inherently CRITICAL / SEVERE with extreme trauma and multi-lane blockage hazards.\n\n"
        "2. COLLISION & INCIDENT CLASSIFICATION:\n"
        "   - Look VERY CLOSELY at the visual evidence across ALL frames — particularly the transition between Clip 1 and Clip 2!\n"
        "   - Look for ANY of these collision indicators:\n"
        "     * Physical contact/impact between vehicles, barriers, or structures\n"
        "     * Sudden trajectory change — a vehicle that was moving and then abruptly stops, spins, or changes direction\n"
        "     * Crumple zone deformation, crushed panels, broken glass, deployed airbags\n"
        "     * Vehicle debris: bumper parts, glass, tire rubber, body panels on the road\n"
        "     * Dust/smoke cloud from point of impact\n"
        "     * Vehicles in abnormal positions: angled across lanes, on the curb, sideways, overturned\n"
        "     * Underride (car under trailer), jackknifing, vehicle rollovers, or pinning\n"
        "     * Skid marks, ABS activation visible as rapid wheel lock\n"
        "     * Any vehicle at a standstill at an unusual location after movement\n"
        "   - CRITICAL RULE: If any of the above indicators are visible in ANY frame → set is_actual_accident=true.\n"
        "   - If heavy trucks/buses are involved in a collision → severity MUST be 'CRITICAL' or 'MODERATE'.\n"
        "   - HIGH-DENSITY INTERSECTION RULE: If the footage shows a busy multi-lane intersection or junction with high\n"
        "     vehicle density and the edge AI flagged an energy spike (confidence > 0.60), look extra carefully for\n"
        "     any vehicles that abruptly stop or change direction mid-frame — this almost always indicates impact,\n"
        "     NOT normal traffic flow. Do NOT dismiss it as 'normal traffic' without carefully checking every frame.\n"
        "   - Only set is_actual_accident=false and is_near_miss=true if vehicles CLEARLY performed successful evasive\n"
        "     maneuvers with ZERO physical impact, NO debris, NO abnormal vehicle positions, and all vehicles\n"
        "     continued moving normally after the close approach.\n\n"
        "3. STRUCTURED FORENSIC RESPONSE:\n"
        "Respond ONLY with a valid JSON object in this exact schema:\n"
        "{\n"
        '  "is_actual_accident": true,\n'
        '  "is_near_miss": false,\n'
        '  "confidence": 0.95,\n'
        '  "severity": "CRITICAL",\n'
        '  "collision_type": "T_BONE_BROADSIDE",\n'
        '  "vehicles_involved": "Heavy Commercial Semi-Truck (Trailer) and Passenger Car",\n'
        '  "heavy_vehicle_involved": true,\n'
        '  "entrapment_risk": true,\n'
        '  "road_condition": "DRY",\n'
        '  "obstruction_detected": true,\n'
        '  "rationale": "Clear forensic explanation describing vehicles, impact physics, and severity."\n'
        "}\n\n"
        "Do NOT include any text outside the JSON object."
    )


class GeminiVerifier:
    """Async Gemini-powered incident verifier using google.genai SDK."""

    def __init__(self):
        self._client = None
        self._configured = False

        if not GEMINI_AVAILABLE:
            logger.warning("google-genai not installed — Gemini verification disabled (fail-open)")
            return

        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — Gemini verification disabled (fail-open)")
            return

        try:
            self._client = genai.Client(api_key=api_key)
            self._configured = True
            logger.info(f"Gemini verifier ready — model: {settings.GEMINI_MODEL}")
        except Exception as e:
            logger.error(f"Gemini client init failed: {e}")

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def verify(self, report: IncidentReport) -> VerificationResult:
        """Verify an incident using Gemini multimodal API. Fail-open on any error."""
        start = time.monotonic()
        result = VerificationResult(event_id=report.event_id)

        if not self._configured or self._client is None:
            logger.warning(f"[{report.event_id}] Gemini not configured — FAIL OPEN")
            result.fail_open = True
            result.is_accident = True
            result.is_near_miss = report.is_near_miss
            result.confidence = report.edge_confidence
            result.rationale = "Gemini API not configured — dispatching on edge confidence (fail-open safety policy)"
            result.severity = "UNKNOWN"
            result.verification_time_ms = (time.monotonic() - start) * 1000
            return result

        try:
            # Build content parts list
            contents: list = []
            prompt_text = _build_prompt(report)
            contents.append(prompt_text)

            # Add frames from Clip 1 (keep max 5 frames for fast inference)
            if report.clip1_path and Path(report.clip1_path).exists():
                frames1 = _extract_jpeg_frames(report.clip1_path, max_frames=5)
                for jpeg in frames1:
                    contents.append(
                        genai_types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
                    )
                if frames1:
                    contents.append("\n[Above: Clip 1 — Impact window frames]\n")

            # Add frames from Clip 2 (keep max 3 frames)
            if report.clip2_path and Path(report.clip2_path).exists():
                frames2 = _extract_jpeg_frames(report.clip2_path, max_frames=3)
                for jpeg in frames2:
                    contents.append(
                        genai_types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
                    )
                if frames2:
                    contents.append("\n[Above: Clip 2 — Aftermath frames]\n")

            # Candidate model fallback chain
            models_to_try = []
            for m in [settings.GEMINI_MODEL, "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]:
                if m and m not in models_to_try:
                    models_to_try.append(m)

            response = None
            last_err = None
            for model_name in models_to_try:
                try:
                    response = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=model_name,
                            contents=contents,
                        ),
                        timeout=settings.GEMINI_TIMEOUT,
                    )
                    if response and response.text:
                        logger.info(f"[{report.event_id}] Gemini inference succeeded using model: {model_name}")
                        break
                except Exception as ex:
                    last_err = ex
                    logger.warning(f"[{report.event_id}] Gemini model {model_name} failed: {ex}. Trying next...")

            if response is None or not response.text:
                raise last_err or RuntimeError("No response returned from Gemini API models")

            raw = response.text.strip()
            result.raw_response = raw

            # Strip markdown code fences if present
            if "```" in raw:
                # Find content between the first ``` and closing ```
                parts = raw.split("```")
                if len(parts) >= 2:
                    code_block = parts[1]
                    if code_block.startswith("json"):
                        code_block = code_block[4:]
                    raw = code_block.strip()

            parsed = json.loads(raw.strip())

            result.is_accident = bool(parsed.get("is_actual_accident", False))
            result.is_near_miss = bool(parsed.get("is_near_miss", False))
            result.confidence = float(parsed.get("confidence", 0.0))
            result.severity = str(parsed.get("severity", "UNKNOWN")).upper()
            result.collision_type = str(parsed.get("collision_type", "UNKNOWN"))
            result.vehicles_involved = str(parsed.get("vehicles_involved", "Unknown"))
            result.heavy_vehicle_involved = bool(parsed.get("heavy_vehicle_involved", False))
            result.entrapment_risk = bool(parsed.get("entrapment_risk", False))
            result.road_condition = str(parsed.get("road_condition", "UNKNOWN"))
            result.obstruction_detected = bool(parsed.get("obstruction_detected", False))
            result.rationale = str(parsed.get("rationale", ""))

            # PHYSICAL CONSISTENCY ENFORCEMENT:
            # 1. If an actual collision/rollover occurred, it is strictly NOT a near miss
            if result.is_accident:
                result.is_near_miss = False

            # 2. Physics-informed momentum scaling:
            # High-mass heavy commercial vehicles carry massive kinetic momentum (p = mv).
            # A collision involving heavy trucks or buses must never be downplayed to MINOR.
            if result.is_accident and result.heavy_vehicle_involved:
                if result.severity not in ("CRITICAL", "MODERATE"):
                    result.severity = "CRITICAL"

            logger.info(
                f"[{report.event_id}] Gemini Verified: accident={result.is_accident}, "
                f"near_miss={result.is_near_miss}, heavy={result.heavy_vehicle_involved}, "
                f"vehicles='{result.vehicles_involved}', severity={result.severity}, "
                f"confidence={result.confidence:.2f}"
            )

        except asyncio.TimeoutError:
            logger.error(f"[{report.event_id}] Gemini API TIMEOUT after {settings.GEMINI_TIMEOUT}s — FAIL OPEN")
            result.fail_open = True
            result.is_accident = True
            result.is_near_miss = False
            result.confidence = report.edge_confidence
            result.vehicles_involved = report.vehicle_type or "Unknown"
            result.heavy_vehicle_involved = bool(report.vehicle_type and any(h in report.vehicle_type.lower() for h in ["truck", "trailer", "bus", "semi", "lorry"]))
            result.rationale = f"Gemini API timed out after {settings.GEMINI_TIMEOUT}s — dispatching on edge confidence (fail-open safety policy)"
            result.severity = "CRITICAL" if result.heavy_vehicle_involved else "MODERATE"

        except json.JSONDecodeError as e:
            logger.error(f"[{report.event_id}] Gemini JSON parse error: {e} — FAIL OPEN")
            result.fail_open = True
            result.is_accident = True
            result.is_near_miss = False
            result.confidence = report.edge_confidence
            result.vehicles_involved = report.vehicle_type or "Unknown"
            result.heavy_vehicle_involved = bool(report.vehicle_type and any(h in report.vehicle_type.lower() for h in ["truck", "trailer", "bus", "semi", "lorry"]))
            result.rationale = f"Gemini response parse error — dispatching on edge confidence (fail-open). Raw: {result.raw_response[:200]}"
            result.severity = "CRITICAL" if result.heavy_vehicle_involved else "MODERATE"

        except Exception as e:
            logger.error(f"[{report.event_id}] Gemini API error: {e} — FAIL OPEN")
            result.fail_open = True
            result.is_accident = True
            result.is_near_miss = False
            result.confidence = report.edge_confidence
            result.vehicles_involved = report.vehicle_type or "Unknown"
            result.heavy_vehicle_involved = bool(report.vehicle_type and any(h in report.vehicle_type.lower() for h in ["truck", "trailer", "bus", "semi", "lorry"]))
            result.rationale = f"Gemini API error ({type(e).__name__}) — dispatching on edge confidence (fail-open safety policy)"
            result.severity = "CRITICAL" if result.heavy_vehicle_involved else "MODERATE"

        result.verification_time_ms = (time.monotonic() - start) * 1000
        return result


# Module-level singleton
_verifier_instance: Optional[GeminiVerifier] = None


def get_verifier() -> GeminiVerifier:
    global _verifier_instance
    if _verifier_instance is None:
        _verifier_instance = GeminiVerifier()
    return _verifier_instance
