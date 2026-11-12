"""
Cognetra 3.0 — Upload API Router
Accepts multipart/form-data clip uploads from edge clients.
"""
import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from models.database import get_db, Incident, IncidentStatus
from services.incident_service import upsert_incident, process_incident
from services.event_bus import get_event_bus
from sqlalchemy import select

logger = logging.getLogger("cognetra.api.upload")
router = APIRouter()

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
EVENT_ID_REGEX = re.compile(r"^[A-Za-z0-9_\-]+$")


@router.post("/upload_incident", status_code=202)
async def upload_incident(
    clip: UploadFile = File(...),
    event_id: str = Form(...),
    clip_number: int = Form(...),
    camera_id: str = Form(default="unknown"),
    source_video: str = Form(default="unknown"),
    candidate_timestamp: float = Form(default=0.0),
    edge_confidence: float = Form(default=0.0),
    speed: Optional[float] = Form(default=None),
    vehicle_type: Optional[str] = Form(default=None),
    event_type: str = Form(default="STANDARD"),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept a clip upload from an edge client.
    
    - Validates event_id format and clip_number
    - Saves clip to disk under /clips/{event_id}/
    - Upserts incident record in DB
    - If both clips received (or edge bypass), schedules background verification
    """
    bus = get_event_bus()

    # Validate event_id
    if not EVENT_ID_REGEX.match(event_id) or len(event_id) > 64:
        raise HTTPException(status_code=422, detail=f"Invalid event_id format: {event_id}")

    # Validate clip_number
    if clip_number not in (1, 2):
        raise HTTPException(status_code=422, detail="clip_number must be 1 or 2")

    # Validate file extension
    suffix = Path(clip.filename or "clip.mp4").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {suffix}")

    # Save clip to disk
    clip_dir = Path(settings.CLIPS_DIR) / event_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_filename = f"clip{clip_number}{suffix}"
    clip_path = str(clip_dir / clip_filename)

    try:
        content = await clip.read()
        with open(clip_path, "wb") as f:
            f.write(content)
        logger.info(f"[{event_id}] Clip {clip_number} saved: {clip_path} ({len(content):,} bytes)")
    except Exception as e:
        logger.error(f"[{event_id}] Failed to save clip {clip_number}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save clip to disk")

    # Upsert incident
    incident = await upsert_incident(
        db=db,
        event_id=event_id,
        clip_number=clip_number,
        clip_path=clip_path,
        camera_id=camera_id,
        source_video=source_video,
        candidate_timestamp=candidate_timestamp,
        edge_confidence=edge_confidence,
        speed=speed,
        vehicle_type=vehicle_type,
        event_type=event_type,
    )

    # Broadcast SSE update
    await bus.broadcast("INCIDENT_UPDATE", {
        "event_id": event_id,
        "clip_number": clip_number,
        "status": incident.status.value,
        "camera_id": camera_id,
        "edge_confidence": edge_confidence,
        "event_type": event_type,
    })

    # Trigger background verification when ready.
    # IMPORTANT: Always wait for BOTH clips before running Gemini verification.
    # The edge_confidence bypass has been removed to prevent:
    #   1. Double-verification (bypass fires on Clip1, then again when Clip2 arrives)
    #   2. Inaccurate verdicts (Gemini missing the aftermath frames from Clip2)
    # The grace timer below handles the rare case where Clip2 never arrives.
    should_verify = incident.clip1_received and incident.clip2_received

    if should_verify:
        logger.info(f"[{event_id}] Both clips received — scheduling Gemini verification")
        asyncio.create_task(process_incident(event_id))
    elif clip_number == 1 and not incident.clip2_received:
        # Grace timer: if Clip 2 doesn't arrive within 5 seconds (e.g. short video), proceed with Clip 1
        async def _grace_verify(ev_id: str):
            await asyncio.sleep(5.0)
            from sqlalchemy import select
            from models.database import AsyncSessionLocal, Incident, IncidentStatus
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(Incident).where(Incident.event_id == ev_id))
                inc = res.scalar_one_or_none()
                if inc and inc.status == IncidentStatus.PENDING_CLIP2:
                    logger.info(f"[{ev_id}] Grace window expired — verifying with available Clip 1 footage")
                    await process_incident(ev_id)
        asyncio.create_task(_grace_verify(event_id))

    return {
        "status": "accepted",
        "event_id": event_id,
        "clip_number": clip_number,
        "incident_status": incident.status.value,
        "clip_path": clip_path,
    }


@router.post("/incidents/{event_id}/clip2_skipped")
async def report_clip2_skipped(event_id: str):
    """Called by edge client when aftermath clip cannot be cut (e.g. video too short)."""
    logger.info(f"[{event_id}] Edge client reported Clip 2 skipped")
    async def _wait_and_verify(ev_id: str):
        from sqlalchemy import select
        from models.database import AsyncSessionLocal, Incident, IncidentStatus
        # Wait up to 6 seconds for Clip 1 upload to register in DB
        for _ in range(6):
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(Incident).where(Incident.event_id == ev_id))
                inc = res.scalar_one_or_none()
                if inc:
                    logger.info(f"[{ev_id}] Incident found in DB — starting immediate Clip 1 verification")
                    await process_incident(ev_id)
                    return
            await asyncio.sleep(1.0)
        logger.warning(f"[{ev_id}] Incident record not found after 6s in clip2_skipped")
    asyncio.create_task(_wait_and_verify(event_id))
    return {"status": "ok", "event_id": event_id}
