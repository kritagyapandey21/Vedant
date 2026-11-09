"""Renews the Pocket Option token from saved cookies when the data feed has had no data for a while.

Run every few minutes by tradingsignal-po-renew.timer, outside the web service: renewal needs a Chrome (~1 GB) and the
web service is limited to 500 MB. The feed picks the new token up from PO_TOKEN_FILE on its next reconnect, no restart.

Saved cookies stop working when that browser logs out of Pocket Option. The job then waits until the cookie file
changes (copy fresh cookies with Cookie-Editor: Export, Header String).
"""

import json
import logging
import os
import sys
import time
import urllib.request
from typing import Optional

import po_login

STATUS_URL = os.environ.get("PO_STATUS_URL", "http://127.0.0.1:8001/relay-status")
COOKIE_FILE = os.environ.get("PO_COOKIE_FILE", "/var/lib/tradingsignal/po_cookies.json")
TOKEN_FILE = os.environ.get("PO_TOKEN_FILE", "/var/lib/tradingsignal/po_token.txt")
STATE_FILE = os.environ.get("PO_RENEW_STATE", "/var/lib/tradingsignal/po_renew_state.json")
DOWN_AFTER = int(os.environ.get("PO_RENEW_DOWN_AFTER", "300"))        # seconds without data before renewing
MIN_INTERVAL = int(os.environ.get("PO_RENEW_MIN_INTERVAL", "900"))    # the feed needs time to reconnect with a new token
RENEW_TIMEOUT = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s - po_renew - %(levelname)s - %(message)s")
logger = logging.getLogger("po_renew")


def feed_is_fresh() -> Optional[bool]:
    """True/False from the feed's /relay-status, None if the web service did not answer (systemd restarts it)."""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=10) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("relay_active"))
    except Exception as e:
        logger.warning("feed status not available (%s)", type(e).__name__)
        return None


def mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    po_login.write_private(STATE_FILE, json.dumps(state))


def run(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    state = load_state()
    fresh = feed_is_fresh()
    if fresh is None:
        return 0
    if fresh:
        if "down_since" in state:
            logger.info("feed has data again")
            state.pop("down_since")
            save_state(state)
        return 0

    state.setdefault("down_since", now)
    save_state(state)
    if now - state["down_since"] < DOWN_AFTER:
        return 0
    if state.get("dead_cookies") is not None and state["dead_cookies"] == mtime(COOKIE_FILE):
        logger.error("no data and the saved cookies are logged out: copy fresh cookies to %s", COOKIE_FILE)
        return po_login.COOKIES_EXPIRED
    if now - state.get("last_try", 0) < MIN_INTERVAL:
        return 0

    state["last_try"] = now
    save_state(state)
    logger.info("no data for %d s, renewing the token from saved cookies", now - state["down_since"])
    try:
        status, frame = po_login.renew(COOKIE_FILE, True, RENEW_TIMEOUT)
    except Exception as e:
        logger.error("renewal crashed (%s), will try again later", type(e).__name__)
        return 1
    if status == 0:
        po_login.write_private(TOKEN_FILE, frame)
        state.pop("dead_cookies", None)
        save_state(state)
        logger.info("new token saved for account %s, the feed uses it on its next reconnect",
                    (po_login.parse_auth(frame) or {}).get("uid"))
        return 0
    if status == po_login.COOKIES_EXPIRED:
        state["dead_cookies"] = mtime(COOKIE_FILE)
        save_state(state)
        logger.error("the saved cookies are logged out: copy fresh cookies to %s", COOKIE_FILE)
    else:
        logger.error("renewal failed with status %s, will try again later", status)
    return status


if __name__ == "__main__":
    sys.exit(run())
