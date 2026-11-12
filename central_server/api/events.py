"""
Cognetra 3.0 — SSE Events API Router
Streams real-time incident updates to dashboard clients.
"""
import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services.event_bus import get_event_bus
from config.settings import settings

logger = logging.getLogger("cognetra.api.events")
router = APIRouter()


@router.get("/events")
async def sse_events():
    """
    Server-Sent Events stream for the live dashboard.
    Connect once; events are pushed as they occur.
    """
    bus = get_event_bus()
    q = await bus.subscribe()

    async def event_generator():
        try:
            async for chunk in bus.stream(q, keepalive_interval=settings.SSE_KEEPALIVE_INTERVAL):
                yield chunk
        finally:
            await bus.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
