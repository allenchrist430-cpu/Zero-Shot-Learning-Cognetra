"""
Cognetra 3.0 — Dashboard REST API (telemetry, incidents, status)
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db, Incident, Verification, DispatchLog, IncidentStatus
from verifier.gemini_verifier import get_verifier
from services.telegram_service import get_telegram_service
from services.event_bus import get_event_bus
from config.settings import settings

logger = logging.getLogger("cognetra.api.dashboard")
router = APIRouter()


@router.get("/status")
async def system_status(db: AsyncSession = Depends(get_db)):
    """Full system telemetry snapshot."""
    # DB health
    try:
        await db.execute(select(func.count()).select_from(Incident))
        db_status = "OK"
    except Exception as e:
        db_status = f"ERROR: {e}"

    # Count incidents
    result = await db.execute(
        select(func.count()).select_from(Incident).where(
            Incident.status.in_([IncidentStatus.VERIFYING, IncidentStatus.PENDING_CLIP2])
        )
    )
    active_count = result.scalar_one_or_none() or 0

    result2 = await db.execute(select(func.count()).select_from(Incident))
    total_incidents = result2.scalar_one_or_none() or 0

    # Counts by verified status:
    # Confirmed collisions: VERIFIED_ACCIDENT or DISPATCHED where verified as accident
    accidents_cnt = (await db.execute(
        select(func.count()).select_from(Incident).where(
            Incident.status.in_([IncidentStatus.VERIFIED_ACCIDENT, IncidentStatus.DISPATCHED])
        )
    )).scalar_one_or_none() or 0

    dispatched_cnt = (await db.execute(
        select(func.count()).select_from(Incident).where(Incident.status == IncidentStatus.DISPATCHED)
    )).scalar_one_or_none() or 0

    # Near misses: strictly those where status is VERIFIED_NEAR_MISS (never accidents)
    nearmiss_cnt = (await db.execute(
        select(func.count()).select_from(Incident).where(
            Incident.status == IncidentStatus.VERIFIED_NEAR_MISS
        )
    )).scalar_one_or_none() or 0

    false_cnt = (await db.execute(
        select(func.count()).select_from(Incident).where(Incident.status == IncidentStatus.FALSE_POSITIVE)
    )).scalar_one_or_none() or 0

    verifier = get_verifier()
    telegram = get_telegram_service()

    return {
        "server": "cognetra",
        "version": "3.0.0",
        "timestamp": datetime.utcnow().isoformat(),
        "host": settings.HOST,
        "port": settings.PORT,
        "database": db_status,
        "gemini_api": "CONFIGURED" if verifier.is_configured else "NOT_CONFIGURED",
        "telegram": "CONFIGURED" if telegram.is_configured else "NOT_CONFIGURED",
        "active_incidents": active_count,
        "total_incidents": total_incidents,
        "total": total_incidents,
        "accidents": accidents_cnt,
        "confirmed": accidents_cnt,
        "dispatched": dispatched_cnt,
        "near_miss_count": nearmiss_cnt,
        "false_positives": false_cnt,
        "false_alarm": false_cnt,
    }


@router.get("/incidents")
async def list_incidents(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Paginated incident list with optional status filter."""
    query = select(Incident).order_by(desc(Incident.created_at)).limit(limit).offset(offset)
    if status:
        try:
            status_enum = IncidentStatus(status)
            query = query.where(Incident.status == status_enum)
        except ValueError:
            pass

    result = await db.execute(query)
    incidents = result.scalars().all()

    # Batch-fetch verifications and dispatches for returned incident IDs
    event_ids = [i.event_id for i in incidents]
    verif_map = {}
    disp_map = {}
    if event_ids:
        vr = await db.execute(select(Verification).where(Verification.event_id.in_(event_ids)))
        for v in vr.scalars().all():
            verif_map[v.event_id] = {
                "is_accident": v.is_accident,
                "is_near_miss": v.is_near_miss,
                "confidence": v.confidence,
                "gemini_confidence": v.confidence,
                "severity": v.severity,
                "collision_type": v.collision_type,
                "vehicles_involved": getattr(v, "vehicles_involved", "Unknown") or "Unknown",
                "heavy_vehicle_involved": bool(getattr(v, "heavy_vehicle_involved", False)),
                "entrapment_risk": bool(getattr(v, "entrapment_risk", False)),
                "rationale": v.rationale,
                "road_condition": v.road_condition,
                "obstruction_detected": v.obstruction_detected,
                "verified_at": v.verified_at.isoformat() if v.verified_at else None,
            }
        dr = await db.execute(select(DispatchLog).where(DispatchLog.event_id.in_(event_ids)))
        for d in dr.scalars().all():
            disp_map[d.event_id] = {
                "channel": d.channel,
                "dispatch_type": d.dispatch_type,
                "status": d.status,
                "message_id": d.message_id,
                "telegram_sent": bool(d.message_id),
                "dispatched_at": d.dispatched_at.isoformat() if d.dispatched_at else None,
            }

    return {
        "incidents": [
            {
                "event_id": i.event_id,
                "camera_id": i.camera_id,
                "source_video": i.source_video,
                "status": i.status.value,
                "event_type": i.event_type,
                "edge_confidence": i.edge_confidence,
                "clip1_received": i.clip1_received,
                "clip2_received": i.clip2_received,
                "clip1_path": i.clip1_path,
                "clip2_path": i.clip2_path,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "updated_at": i.updated_at.isoformat() if i.updated_at else None,
                "verification": verif_map.get(i.event_id),
                "dispatch": disp_map.get(i.event_id),
            }
            for i in incidents
        ],
        "total": len(incidents),
        "offset": offset,
        "limit": limit,
    }


@router.get("/incidents/{event_id}")
async def get_incident_detail(event_id: str, db: AsyncSession = Depends(get_db)):
    """Full incident detail including verification and dispatch records."""
    result = await db.execute(select(Incident).where(Incident.event_id == event_id))
    incident = result.scalar_one_or_none()

    if not incident:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Incident {event_id} not found")

    # Get verification
    vr = await db.execute(select(Verification).where(Verification.event_id == event_id))
    verification = vr.scalar_one_or_none()

    # Get dispatch logs
    dl = await db.execute(select(DispatchLog).where(DispatchLog.event_id == event_id))
    dispatches = dl.scalars().all()

    return {
        "incident": {
            "event_id": incident.event_id,
            "camera_id": incident.camera_id,
            "source_video": incident.source_video,
            "candidate_timestamp": incident.candidate_timestamp,
            "edge_confidence": incident.edge_confidence,
            "speed": incident.speed,
            "vehicle_type": incident.vehicle_type,
            "event_type": incident.event_type,
            "status": incident.status.value,
            "clip1_path": incident.clip1_path,
            "clip2_path": incident.clip2_path,
            "clip1_received": incident.clip1_received,
            "clip2_received": incident.clip2_received,
            "created_at": incident.created_at.isoformat() if incident.created_at else None,
            "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        },
        "verification": {
            "is_accident": verification.is_accident,
            "is_near_miss": verification.is_near_miss,
            "confidence": verification.confidence,
            "severity": verification.severity,
            "collision_type": verification.collision_type,
            "vehicles_involved": getattr(verification, "vehicles_involved", "Unknown") or "Unknown",
            "heavy_vehicle_involved": bool(getattr(verification, "heavy_vehicle_involved", False)),
            "entrapment_risk": bool(getattr(verification, "entrapment_risk", False)),
            "rationale": verification.rationale,
            "road_condition": verification.road_condition,
            "obstruction_detected": verification.obstruction_detected,
            "verified_at": verification.verified_at.isoformat() if verification.verified_at else None,
        } if verification else None,
        "dispatches": [
            {
                "channel": d.channel,
                "dispatch_type": d.dispatch_type,
                "status": d.status,
                "message_id": d.message_id,
                "dispatched_at": d.dispatched_at.isoformat() if d.dispatched_at else None,
            }
            for d in dispatches
        ],
    }


@router.api_route("/system/reset", methods=["GET", "POST"])
async def reset_system_data(db: AsyncSession = Depends(get_db)):
    """
    Reset all database tables (incidents, verifications, dispatch logs)
    to restore stats and UI back to 0.
    """
    logger.info("Resetting all system telemetry and database records to 0")
    await db.execute(delete(DispatchLog))
    await db.execute(delete(Verification))
    await db.execute(delete(Incident))
    await db.commit()

    bus = get_event_bus()
    await bus.broadcast("SYSTEM_RESET", {
        "status": "reset",
        "message": "All database records have been purged. Counters reset to 0.",
        "timestamp": datetime.utcnow().isoformat(),
    })

    return {
        "status": "OK",
        "message": "Database successfully purged. All metrics reset to 0.",
        "total_incidents": 0,
        "confirmed": 0,
        "near_miss": 0,
        "dispatched": 0,
        "false_alarm": 0,
    }
