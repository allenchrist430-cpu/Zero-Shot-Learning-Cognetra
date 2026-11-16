"""
Cognetra 3.0 — Telegram Emergency Dispatch Service
Handles standard accidents, predictive warnings, and near-miss alerts.
python-telegram-bot v20+ async compatible with HTML parse mode and plain-text fallback.
"""
import asyncio
import html
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cognetra.telegram")

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed — Telegram dispatch disabled")

from config.settings import settings


class TelegramService:
    """
    Sends emergency alerts to configured Telegram chat.
    Supports:
      - Standard accident dispatch with Dispatch Ambulance / False Alarm buttons
      - Predictive warning / near-miss alert (non-ambulance routing)
    """

    def __init__(self):
        self._bot: Optional[object] = None
        self._configured = False
        self._dispatch_lock = asyncio.Lock()

        if not TELEGRAM_AVAILABLE:
            logger.warning("python-telegram-bot not installed — Telegram disabled")
            return

        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID

        if not token or not chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — alerts will be logged only")
            return

        try:
            self._bot = Bot(token=token)
            self._configured = True
            logger.info(f"Telegram service initialized — chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Telegram init failed: {e}")

    @property
    def is_configured(self) -> bool:
        return self._configured

    # ──────────────────────────────────────────────────────────────────────
    # Public dispatch entry point
    # ──────────────────────────────────────────────────────────────────────

    async def dispatch_accident_alert(
        self,
        event_id: str,
        severity: str,
        collision_type: str,
        confidence: float,
        rationale: str,
        clip1_path: Optional[str] = None,
        is_fail_open: bool = False,
        is_near_miss: bool = False,
        is_predictive: bool = False,
        camera_id: str = "unknown",
        source_video: str = "unknown",
        vehicles_involved: str = "Unknown",
        heavy_vehicle_involved: bool = False,
        entrapment_risk: bool = False,
    ) -> Optional[str]:
        """Dispatch the appropriate alert. Returns Telegram message_id or None."""
        if not self._configured:
            self._log_fallback(event_id, severity, is_near_miss, is_predictive, rationale)
            return None

        try:
            async with self._dispatch_lock:
                # Route to near miss ONLY if it was explicitly verified as near miss with no collision
                if is_near_miss and not is_fail_open:
                    res = await self._send_near_miss_alert(
                        event_id=event_id, severity=severity, confidence=confidence,
                        rationale=rationale, clip1_path=clip1_path,
                        is_predictive=is_predictive, camera_id=camera_id,
                        source_video=source_video,
                        vehicles_involved=vehicles_involved,
                        heavy_vehicle_involved=heavy_vehicle_involved,
                    )
                else:
                    res = await self._send_accident_alert(
                        event_id=event_id, severity=severity, collision_type=collision_type,
                        confidence=confidence, rationale=rationale, clip1_path=clip1_path,
                        is_fail_open=is_fail_open, camera_id=camera_id,
                        source_video=source_video,
                        vehicles_involved=vehicles_involved,
                        heavy_vehicle_involved=heavy_vehicle_involved,
                        entrapment_risk=entrapment_risk,
                    )
                # Small pause to guarantee delivery order at Telegram servers
                await asyncio.sleep(0.5)
                return res
        except Exception as e:
            logger.error(f"[{event_id}] Telegram dispatch failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _send_accident_alert(
        self, event_id, severity, collision_type, confidence,
        rationale, clip1_path, is_fail_open, camera_id, source_video,
        vehicles_involved="Unknown", heavy_vehicle_involved=False, entrapment_risk=False,
    ) -> Optional[str]:
        """Send a full accident emergency alert with inline action buttons."""
        severity_emoji = {"CRITICAL": "🔴", "MODERATE": "🟠", "MINOR": "🟡"}.get(severity, "⚠️")
        fail_open_note = "\n⚡ <i>Auto-dispatched (Gemini API unavailable — fail-open)</i>" if is_fail_open else ""

        heavy_hazard = ""
        if heavy_vehicle_involved:
            heavy_hazard = (
                "\n🚛 <b>MOMENTUM HAZARD:</b> <b>HEAVY COMMERCIAL VEHICLE INVOLVED</b>\n"
                "⚠️ <i>Massive vehicle momentum (p=m·v) — Severe cabin deformation & multi-lane obstruction hazard!</i>\n"
            )
        entrapment_alert = ""
        if entrapment_risk:
            entrapment_alert = (
                "\n🆘 <b>HIGH ENTRAPMENT RISK:</b> <i>Immediate hydraulic extrication rescue team requested!</i>\n"
            )

        message = (
            f"🚨 <b>COGNETRA EMERGENCY ACCIDENT ALERT</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Event ID:</b> <code>{html.escape(str(event_id))}</code>\n"
            f"📷 <b>Camera:</b> {html.escape(str(camera_id))}\n"
            f"📹 <b>Source:</b> {html.escape(os.path.basename(str(source_video)))}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚗 <b>Vehicles:</b> {html.escape(str(vehicles_involved))}\n"
            f"{severity_emoji} <b>Severity:</b> <b>{html.escape(str(severity))}</b>\n"
            f"💥 <b>Collision:</b> {html.escape(str(collision_type))}\n"
            f"🎯 <b>AI Confidence:</b> {confidence * 100:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━{heavy_hazard}{entrapment_alert}\n"
            f"🔍 <b>Forensic Analysis:</b>\n<i>{html.escape(str(rationale))}</i>\n"
            f"{fail_open_note}\n\n"
            f"⚡ <b>Immediate action required:</b>"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚑 Dispatch Ambulance", callback_data=f"dispatch:{event_id}"),
                InlineKeyboardButton("❌ False Alarm", callback_data=f"false_alarm:{event_id}"),
            ],
            [
                InlineKeyboardButton(
                    "📊 View Dashboard",
                    url=f"http://127.0.0.1:{settings.PORT}/monitor"
                )
            ]
        ])

        sent_msg = await self._send_with_retry(
            chat_id=int(settings.TELEGRAM_CHAT_ID),
            text=message,
            reply_markup=keyboard,
        )

        # Immediately attach Clip 1 video linked to this alert message
        if clip1_path and Path(clip1_path).exists():
            reply_id = sent_msg.message_id if sent_msg else None
            await self._send_video_clip(clip1_path, event_id, "Impact Evidence (Clip 1)", reply_to_message_id=reply_id)

        return str(sent_msg.message_id) if sent_msg else None

    async def _send_near_miss_alert(
        self, event_id, severity, confidence, rationale,
        clip1_path, is_predictive, camera_id, source_video,
        vehicles_involved="Unknown", heavy_vehicle_involved=False,
    ) -> Optional[str]:
        """Send a predictive warning / near-miss alert."""
        label = "PREDICTIVE ANOMALY" if is_predictive else "NEAR-MISS INCIDENT"
        emoji = "⚠️"

        heavy_note = ""
        if heavy_vehicle_involved:
            heavy_note = "\n🚛 <i>Heavy commercial vehicle involved in dynamic evasive event.</i>\n"

        message = (
            f"{emoji} <b>COGNETRA {label}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Event ID:</b> <code>{html.escape(str(event_id))}</code>\n"
            f"📷 <b>Camera:</b> {html.escape(str(camera_id))}\n"
            f"📹 <b>Source:</b> {html.escape(os.path.basename(str(source_video)))}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚗 <b>Vehicles:</b> {html.escape(str(vehicles_involved))}\n"
            f"⚠️ <b>Severity:</b> {html.escape(str(severity))}\n"
            f"🎯 <b>AI Confidence:</b> {confidence * 100:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━{heavy_note}\n"
            f"🔍 <b>Forensic Analysis:</b>\n<i>{html.escape(str(rationale))}</i>\n\n"
            f"⚡ <b>Action required:</b>"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚔 Alert Traffic Control", callback_data=f"alert_traffic:{event_id}"),
                InlineKeyboardButton("✅ Acknowledge", callback_data=f"acknowledge:{event_id}"),
            ],
            [
                InlineKeyboardButton(
                    "📊 View Dashboard",
                    url=f"http://127.0.0.1:{settings.PORT}/monitor"
                )
            ]
        ])

        sent_msg = await self._send_with_retry(
            chat_id=int(settings.TELEGRAM_CHAT_ID),
            text=message,
            reply_markup=keyboard,
        )

        if clip1_path and Path(clip1_path).exists():
            reply_id = sent_msg.message_id if sent_msg else None
            await self._send_video_clip(clip1_path, event_id, "Near-Miss Evidence (Clip 1)", reply_to_message_id=reply_id)

        return str(sent_msg.message_id) if sent_msg else None

    async def _send_with_retry(self, chat_id: int, text: str, reply_markup=None, max_retries: int = 3):
        """Send a Telegram message with exponential backoff retry and plain-text fallback."""
        for attempt in range(max_retries):
            try:
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                # If HTML parse error, retry immediately without formatting
                if "parse" in str(e).lower() or "entities" in str(e).lower():
                    logger.warning(f"Telegram HTML parse issue, retrying plain text: {e}")
                    try:
                        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")
                        return await self._bot.send_message(
                            chat_id=chat_id,
                            text=clean_text,
                            parse_mode=None,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True,
                        )
                    except Exception as inner_e:
                        logger.error(f"Telegram plain text fallback failed: {inner_e}")

                wait = 2 ** attempt
                logger.warning(f"Telegram send attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Telegram send failed after {max_retries} attempts: {e}")
        return None

    async def _send_video_clip(self, clip_path: str, event_id: str, caption: str, reply_to_message_id: Optional[int] = None):
        """Attempt to send a video clip to Telegram (non-critical — failures logged only)."""
        try:
            with open(clip_path, "rb") as f:
                await self._bot.send_video(
                    chat_id=int(settings.TELEGRAM_CHAT_ID),
                    video=f,
                    caption=f"📹 {caption}\nEvent: {event_id}",
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=None,
                    read_timeout=60,
                    write_timeout=60,
                    connect_timeout=30,
                )
                logger.info(f"[{event_id}] Video clip sent to Telegram ✓ (reply_to={reply_to_message_id})")
        except Exception as e:
            logger.warning(f"[{event_id}] Could not send video clip to Telegram: {e}")

    def _log_fallback(self, event_id, severity, is_near_miss, is_predictive, rationale):
        """Console-log alert when Telegram is not configured."""
        alert_type = "NEAR-MISS" if is_near_miss else ("PREDICTIVE" if is_predictive else "ACCIDENT")
        logger.warning(
            f"\n{'=' * 60}\n"
            f"[TELEGRAM NOT CONFIGURED — CONSOLE ALERT]\n"
            f"🚨 TYPE: {alert_type}\n"
            f"   Event: {event_id} | Severity: {severity}\n"
            f"   Rationale: {rationale[:200]}\n"
            f"{'=' * 60}"
        )


# Module-level singleton
_telegram_instance: Optional[TelegramService] = None


def get_telegram_service() -> TelegramService:
    global _telegram_instance
    if _telegram_instance is None:
        _telegram_instance = TelegramService()
    return _telegram_instance
