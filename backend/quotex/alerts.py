"""Telegram alerts for the Quotex data feed, sent through a bot so they arrive as normal notifications.

Optional: without QX_ALERT_BOT_TOKEN / QX_ALERT_CHAT_ID the messages only go to the log.
"""

import asyncio
import json
import logging
import urllib.request
from typing import Optional

from config import QX_ALERT_AFTER_SECONDS, QX_ALERT_BOT_TOKEN, QX_ALERT_CHAT_ID, QX_ALERT_REPEAT_SECONDS

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 15


def _send_blocking(text: str) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{QX_ALERT_BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": QX_ALERT_CHAT_ID, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT):
        pass


async def send(text: str) -> None:
    """Never raises: a failed alert must not take the feed down."""
    logger.warning("Alert: %s", text)
    if not (QX_ALERT_BOT_TOKEN and QX_ALERT_CHAT_ID):
        return
    try:
        await asyncio.to_thread(_send_blocking, f"TradingSignal Quotex: {text}")
    except Exception as e:
        # only the type: the request URL holds the bot token
        logger.error("Could not send the Telegram alert (%s)", type(e).__name__)


class DownWatch:
    """Decides when "the data stopped" and "the data is back" are worth a message."""

    def __init__(self, after: float = QX_ALERT_AFTER_SECONDS, repeat: float = QX_ALERT_REPEAT_SECONDS) -> None:
        self.after = after
        self.repeat = repeat
        self.down_since: Optional[float] = None
        self.last_alert: Optional[float] = None

    def check(self, fresh: bool, now: float, last_error: str = "") -> Optional[str]:
        if fresh:
            message = "data is flowing again, signals are back." if self.last_alert is not None else None
            self.down_since = self.last_alert = None
            return message
        if self.down_since is None:
            self.down_since = now
        if now - self.down_since < self.after:
            return None
        if self.last_alert is not None and now - self.last_alert < self.repeat:
            return None
        self.last_alert = now
        return (f"no fresh data for {(now - self.down_since) / 60:.0f} min, signals are down. "
                f"Last error: {last_error or 'none'}")
