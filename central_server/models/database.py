"""
Cognetra 3.0 — SQLAlchemy Database Models
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, Enum as SAEnum
)
from sqlalchemy.ext.asyncio import AsyncAttrs, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


class IncidentStatus(str, enum.Enum):
    PENDING_CLIP2 = "PENDING_CLIP2"
    RECEIVED_BOTH = "RECEIVED_BOTH"
    VERIFYING = "VERIFYING"
    VERIFIED_ACCIDENT = "VERIFIED_ACCIDENT"
    VERIFIED_NEAR_MISS = "VERIFIED_NEAR_MISS"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    DISPATCHED = "DISPATCHED"
    ERROR = "ERROR"


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), unique=True, nullable=False, index=True)
    camera_id = Column(String(128), nullable=True)
    source_video = Column(String(256), nullable=True)
    candidate_timestamp = Column(Float, nullable=True)
    edge_confidence = Column(Float, nullable=True, default=0.0)
    speed = Column(Float, nullable=True)
    vehicle_type = Column(String(64), nullable=True)

    # Clips
    clip1_path = Column(String(512), nullable=True)
    clip2_path = Column(String(512), nullable=True)
    clip1_received = Column(Boolean, default=False)
    clip2_received = Column(Boolean, default=False)

    # Event type flags
    event_type = Column(String(32), default="STANDARD")  # STANDARD | PREDICTIVE_TRIGGER | NEAR_MISS_TRIGGER

    # Status
    status = Column(SAEnum(IncidentStatus), default=IncidentStatus.PENDING_CLIP2, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Verification(Base):
    __tablename__ = "verifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, index=True)

    is_accident = Column(Boolean, nullable=True)
    is_near_miss = Column(Boolean, default=False)
    confidence = Column(Float, nullable=True)
    severity = Column(String(32), nullable=True)
    collision_type = Column(String(64), nullable=True)
    vehicles_involved = Column(String(256), nullable=True)
    heavy_vehicle_involved = Column(Boolean, default=False)
    entrapment_risk = Column(Boolean, default=False)
    rationale = Column(Text, nullable=True)
    road_condition = Column(String(64), nullable=True)
    obstruction_detected = Column(Boolean, default=False)
    raw_response = Column(Text, nullable=True)

    verified_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DispatchLog(Base):
    __tablename__ = "dispatch_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, index=True)
    channel = Column(String(64), nullable=True)
    dispatch_type = Column(String(32), default="AMBULANCE")  # AMBULANCE | NEAR_MISS | FALSE_ALARM
    status = Column(String(32), nullable=True)
    message_id = Column(String(128), nullable=True)
    dispatched_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# Async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


async def init_db():
    """Initialize database tables and run lightweight migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Check and add new columns to verifications table if missing (SQLite migration helper)
        try:
            from sqlalchemy import text
            for col_def in [
                ("vehicles_involved", "VARCHAR(256)"),
                ("heavy_vehicle_involved", "BOOLEAN DEFAULT 0"),
                ("entrapment_risk", "BOOLEAN DEFAULT 0"),
            ]:
                col_name, col_type = col_def
                try:
                    await conn.execute(text(f"ALTER TABLE verifications ADD COLUMN {col_name} {col_type}"))
                except Exception:
                    pass  # column already exists
        except Exception:
            pass


async def get_db():
    """Dependency: yield an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
