"""
Cognetra 3.0 — SSE Event Bus
Manages broadcaster for Server-Sent Events to connected dashboard clients.
"""
import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Any

logger = logging.getLogger("cognetra.sse")


class EventBus:
    """Thread-safe async pub/sub for SSE delivery."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """Subscribe a new SSE client. Returns their personal queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        logger.debug(f"SSE client subscribed. Total: {len(self._subscribers)}")
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        """Remove a subscriber when they disconnect."""
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
        logger.debug(f"SSE client disconnected. Total: {len(self._subscribers)}")

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Broadcast an event to all connected SSE clients."""
        payload = json.dumps({
            "type": event_type,
            "timestamp": time.time(),
            "data": data,
        })
        async with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

        if data:
            logger.debug(f"SSE broadcast: {event_type} → {len(self._subscribers)} clients")

    async def stream(self, q: asyncio.Queue, keepalive_interval: int = 15) -> AsyncGenerator[str, None]:
        """
        Async generator that yields SSE-formatted strings.
        Sends keepalive comments to maintain connection.
        """
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=keepalive_interval)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Send SSE comment as keepalive
                    yield f": keepalive {time.time()}\n\n"
        except asyncio.CancelledError:
            pass


# Module-level singleton
_event_bus: EventBus = EventBus()


def get_event_bus() -> EventBus:
    return _event_bus
