"""
Cognetra 3.0 — Incident State Machine & Verification Orchestrator
Coordinates DB writes, Gemini verification, Telegram dispatch, and SSE broadcasts.
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config.settings import settings
from models.database import (
    Incident, Verification, DispatchLog,
    IncidentStatus, AsyncSessionLocal
)
from verifier.gemini_verifier import get_verifier, IncidentReport
from services.telegram_service import get_telegram_service
from services.event_bus import get_event_bus

logger = logging.getLogger("cognetra.incident")


async def upsert_incident(
    db: AsyncSession,
    event_id: str,
    clip_number: int,
    clip_path: str,
    camera_id: str,
    source_video: str,
    candidate_timestamp: float,
    edge_confidence: float,
    speed: Optional[float],
    vehicle_type: Optional[str],
    event_type: str = "STANDARD",
) -> Incident:
    """Create or update an incident record when a clip arrives."""
    result = await db.execute(select(Incident).where(Incident.event_id == event_id))
    incident = result.scalar_one_or_none()

    if incident is None:
        incident = Incident(
            event_id=event_id,
            camera_id=camera_id,
            source_video=source_video,
            candidate_timestamp=candidate_timestamp,
            edge_confidence=edge_confidence,
            speed=speed,
            vehicle_type=vehicle_type,
            event_type=event_type,
            status=IncidentStatus.PENDING_CLIP2,
        )
        db.add(incident)

    # Update clip paths and received flags
    if clip_number == 1:
        incident.clip1_path = clip_path
        incident.clip1_received = True
    elif clip_number == 2:
        incident.clip2_path = clip_path
        incident.clip2_received = True

    incident.updated_at = datetime.utcnow()

    if incident.clip1_received and incident.clip2_received:
        incident.status = IncidentStatus.RECEIVED_BOTH

    await db.commit()
    await db.refresh(incident)
    return incident


async def process_incident(event_id: str):
    """
    Background task: runs Gemini verification and dispatches alert.
    Called when both clips are received (or on edge_confidence bypass).
    Guard: if the incident is already VERIFYING/VERIFIED/DISPATCHED, skip to prevent
    double-verification caused by both the edge_bypass and the both_clips triggers
    firing for the same event_id.
    """
    bus = get_event_bus()
    verifier = get_verifier()
    telegram = get_telegram_service()

    # --- IDEMPOTENCY GUARD: check and atomically set VERIFYING ---
    _TERMINAL = {
        IncidentStatus.VERIFYING,
        IncidentStatus.VERIFIED_ACCIDENT,
        IncidentStatus.VERIFIED_NEAR_MISS,
        IncidentStatus.FALSE_POSITIVE,
        IncidentStatus.DISPATCHED,
    }

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Incident).where(Incident.event_id == event_id))
        incident = result.scalar_one_or_none()

        if not incident:
            logger.error(f"[{event_id}] Cannot process — incident not found in DB")
            return

        # If already in a terminal or in-progress state, skip — another call already owns it
        if incident.status in _TERMINAL:
            logger.info(
                f"[{event_id}] Skipping duplicate process_incident call "
                f"(current status: {incident.status.value})"
            )
            return

        # Mark VERIFYING atomically before releasing the session
        incident.status = IncidentStatus.VERIFYING
        incident.updated_at = datetime.utcnow()
        await db.commit()

        await bus.broadcast("INCIDENT_UPDATE", {
            "event_id": event_id,
            "status": IncidentStatus.VERIFYING.value,
        })

        logger.info(f"[{event_id}] Starting Gemini verification (type={incident.event_type})")

        # Build report
        report = IncidentReport(
            event_id=event_id,
            clip1_path=incident.clip1_path,
            clip2_path=incident.clip2_path,
            edge_confidence=incident.edge_confidence or 0.0,
            candidate_timestamp=incident.candidate_timestamp or 0.0,
            camera_id=incident.camera_id or "unknown",
            source_video=incident.source_video or "unknown",
            speed=incident.speed,
            vehicle_type=incident.vehicle_type,
            event_type=incident.event_type or "STANDARD",
        )

        # Edge confidence bypass — dispatch immediately while verifying
        bypassed = False
        if incident.edge_confidence and incident.edge_confidence >= settings.EDGE_CONFIDENCE_BYPASS:
            logger.info(f"[{event_id}] Edge confidence {incident.edge_confidence:.2f} >= bypass threshold — dispatching proactively")
            bypassed = True

        # Run Gemini verification
        vr = await verifier.verify(report)

        # Persist verification
        verification = Verification(
            event_id=event_id,
            is_accident=vr.is_accident,
            is_near_miss=vr.is_near_miss,
            confidence=vr.confidence,
            severity=vr.severity,
            collision_type=vr.collision_type,
            vehicles_involved=vr.vehicles_involved,
            heavy_vehicle_involved=vr.heavy_vehicle_involved,
            entrapment_risk=vr.entrapment_risk,
            rationale=vr.rationale,
            road_condition=vr.road_condition,
            obstruction_detected=vr.obstruction_detected,
            raw_response=vr.raw_response[:4096] if vr.raw_response else "",
        )
        db.add(verification)

        # Determine new status:
        # Confirmed collision or fail-open or edge bypass takes absolute priority as VERIFIED_ACCIDENT
        if vr.is_accident or bypassed or vr.fail_open:
            new_status = IncidentStatus.VERIFIED_ACCIDENT
        elif vr.is_near_miss and not vr.is_accident:
            new_status = IncidentStatus.VERIFIED_NEAR_MISS
        else:
            new_status = IncidentStatus.FALSE_POSITIVE

        incident.status = new_status
        incident.updated_at = datetime.utcnow()
        await db.commit()

        await bus.broadcast("VERIFICATION_COMPLETED", {
            "event_id": event_id,
            "status": new_status.value,
            "is_accident": vr.is_accident,
            "is_near_miss": vr.is_near_miss,
            "confidence": vr.confidence,
            "severity": vr.severity,
            "collision_type": vr.collision_type,
            "vehicles_involved": vr.vehicles_involved,
            "heavy_vehicle_involved": vr.heavy_vehicle_involved,
            "entrapment_risk": vr.entrapment_risk,
            "fail_open": vr.fail_open,
        })

        # Dispatch alerts
        should_dispatch = (
            new_status in (IncidentStatus.VERIFIED_ACCIDENT, IncidentStatus.VERIFIED_NEAR_MISS)
            or vr.fail_open
        )

        if should_dispatch:
            is_actual_collision = (new_status == IncidentStatus.VERIFIED_ACCIDENT)
            logger.info(
                f"[{event_id}] Dispatching Telegram alert: collision={is_actual_collision}, "
                f"heavy={vr.heavy_vehicle_involved}, severity={vr.severity}"
            )
            msg_id = await telegram.dispatch_accident_alert(
                event_id=event_id,
                severity=vr.severity,
                collision_type=vr.collision_type,
                confidence=vr.confidence,
                rationale=vr.rationale,
                clip1_path=incident.clip1_path,
                is_fail_open=vr.fail_open,
                is_near_miss=(not is_actual_collision and vr.is_near_miss),
                is_predictive=False,
                camera_id=incident.camera_id or "unknown",
                source_video=incident.source_video or "unknown",
                vehicles_involved=vr.vehicles_involved,
                heavy_vehicle_involved=vr.heavy_vehicle_involved,
                entrapment_risk=vr.entrapment_risk,
            )

            # Log dispatch
            dispatch_type = "AMBULANCE" if is_actual_collision else "NEAR_MISS"
            dispatch_log = DispatchLog(
                event_id=event_id,
                channel="telegram",
                dispatch_type=dispatch_type,
                status="sent" if msg_id else "failed",
                message_id=msg_id,
            )
            db.add(dispatch_log)

            incident.status = IncidentStatus.DISPATCHED
            incident.updated_at = datetime.utcnow()
            await db.commit()

            await bus.broadcast("DISPATCH_SENT", {
                "event_id": event_id,
                "dispatch_type": dispatch_type,
                "message_id": msg_id,
            })
        else:
            logger.info(f"[{event_id}] Classified as FALSE_POSITIVE — no dispatch")
