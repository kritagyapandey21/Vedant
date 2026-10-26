"""
Telegram verification service for the Quotex terminal (same approach as the Pocket Option one).
Sends the trader ID to the Quotex partner bot (@quotexpartnerbot) and reads the reply.
Requires TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING in this folder's .env, and REQUIRE_VERIFICATION=true.

  QUOTEX_BOT_USERNAME    bot to ask, default quotexpartnerbot
  QUOTEX_MIN_DEPOSIT     minimum total deposits in USD, 0 (default) = do not check deposits
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")
BOT_USERNAME = os.environ.get("QUOTEX_BOT_USERNAME", "quotexpartnerbot").lstrip("@")
MIN_DEPOSIT = float(os.environ.get("QUOTEX_MIN_DEPOSIT", "0") or "0")

# When True, login is blocked instead of bypassed if Telegram is not configured.
_REQUIRE_VERIFICATION = os.environ.get("REQUIRE_VERIFICATION", "false").lower() == "true"

_NOT_FOUND = ("not found", "no user", "doesn't exist", "does not exist", "not registered", "no data", "no results", "not your")

_client = None
_lock = asyncio.Lock()


async def _get_client():
    global _client

    if not (API_ID and API_HASH and SESSION_STRING):
        return None
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if _client is None or not _client.is_connected():
            # low retries/timeout so an unreachable Telegram fails inside the reverse proxy's read timeout
            _client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, connection_retries=1, timeout=5)
            await _client.connect()
            if not await _client.is_user_authorized():
                logger.error("Telegram session not authorized")
                _client = None
            else:
                logger.info("Telegram client connected")
    except Exception as e:
        logger.error(f"Telegram client init failed: {e}")
        _client = None
    return _client


def _parse_reply(text: str, trader_id: str) -> Dict:
    """Read the partner bot's answer: the ID must be reported back, and (if QUOTEX_MIN_DEPOSIT is set) deposits must reach it."""
    if not text:
        return {"found": False, "message": "NO RESPONSE FROM BOT"}
    low = text.lower()
    if any(p in low for p in _NOT_FOUND):
        return {"found": False, "message": "TRADER NOT FOUND"}
    if str(trader_id).lower() not in low and "uid" not in low and "id:" not in low:
        return {"found": False, "message": "UNRECOGNISED RESPONSE"}

    if MIN_DEPOSIT > 0:
        match = re.search(r"deposits?[^\d$\n]*\$?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
        if not match:
            return {"found": False, "message": "COULD NOT VERIFY DEPOSIT HISTORY"}
        total = float(match.group(1).replace(",", ""))
        if total < MIN_DEPOSIT:
            return {"found": False, "message": f"ACCESS DENIED, MINIMUM ${MIN_DEPOSIT:g} DEPOSIT REQUIRED (yours: ${total:.2f})"}
    return {"found": True, "message": "TRADER VERIFIED"}


async def verify_trader(trader_id: str) -> Dict:
    """Ask the partner bot about this trader ID. Also answers its anti-robot button, like the Pocket Option service."""
    if not _REQUIRE_VERIFICATION:
        logger.warning("REQUIRE_VERIFICATION=false - login bypass active")
        return {"found": True, "bypassed": True}

    client = await _get_client()
    if client is None:
        logger.warning("Telegram not configured but REQUIRE_VERIFICATION=true - denying login")
        return {"found": False, "message": "VERIFICATION SERVICE NOT CONFIGURED, CONTACT ADMIN"}

    async with _lock:
        try:
            async with client.conversation(BOT_USERNAME, timeout=30) as conv:
                await conv.send_message(str(trader_id))
                reply = await conv.get_response(timeout=15)
                raw = reply.message or ""
                logger.info(f"Bot reply 1: {raw[:200]!r}")

                if "robot" in raw.lower() or "confirm" in raw.lower():          # anti-robot challenge: press the button
                    try:
                        if reply.buttons:
                            await reply.click(0, 0)
                    except Exception as click_err:
                        logger.warning(f"Button click failed: {click_err}")
                    try:
                        nxt = await conv.get_response(timeout=10)
                        nxt_raw = nxt.message or ""
                        logger.info(f"Post-captcha reply: {nxt_raw[:200]!r}")
                        if str(trader_id).lower() in nxt_raw.lower() or "uid" in nxt_raw.lower():
                            raw = nxt_raw
                        else:
                            await asyncio.sleep(1)
                            await conv.send_message(str(trader_id))
                            raw = (await conv.get_response(timeout=15)).message or ""
                    except asyncio.TimeoutError:
                        await conv.send_message(str(trader_id))
                        raw = (await conv.get_response(timeout=15)).message or ""
                    logger.info(f"Bot final reply: {raw[:200]!r}")

                result = _parse_reply(raw, trader_id)
                logger.info(f"Trader {trader_id} verification: {'FOUND' if result['found'] else 'NOT FOUND'}")
                return result
        except asyncio.TimeoutError:
            logger.error("Telegram bot timed out")
            return {"found": False, "message": "VERIFICATION TIMEOUT, TRY AGAIN"}
        except Exception as e:
            logger.error(f"Telegram verification error: {e}")
            return {"found": False, "message": "VERIFICATION SERVICE UNAVAILABLE"}
