"""Signed, expiring session tokens (HMAC-SHA256, no external dependency).

verify-trader issues a token; protected endpoints require `Authorization: Bearer <token>`.

One active session per trader ID (per broker): signing in again, e.g. on another device, makes every older token for
that ID invalid, so the first device gets a 401 on its next request and signs out. The latest session id per ID is
kept in sessions.sqlite3 next to this file so it also holds across backend restarts.

  SESSION_SECRET          set a long random value in .env so tokens survive restarts
                          (if unset a random secret is generated per process: restarts sign everyone out)
  SESSION_TTL_SECONDS     token lifetime, default 12 hours
  REQUIRE_SESSION         "false" disables enforcement (local debugging only), default "true"
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional, Tuple

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_SECRET_ENV = os.environ.get("SESSION_SECRET", "").strip()
if not _SECRET_ENV:
    logger.warning("SESSION_SECRET not set - using a random per-process secret (sessions reset on restart)")
_SECRET = (_SECRET_ENV or secrets.token_hex(32)).encode()
TTL = int(os.environ.get("SESSION_TTL_SECONDS", str(12 * 3600)))
ENFORCE = os.environ.get("REQUIRE_SESSION", "true").lower() != "false"
OTHER_DEVICE = "You were signed out because this ID was used to sign in on another device."
_DB = Path(__file__).with_name("sessions.sqlite3")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (broker TEXT, sub TEXT, sid TEXT, PRIMARY KEY (broker, sub))")
    return conn


def _key(subject: str) -> str:
    return subject.strip().casefold()          # ids are matched case-insensitively at sign in


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())


def issue(subject: str, broker: str) -> str:
    """New token for this ID; it replaces any session the same ID had before."""
    sid = secrets.token_hex(8)
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sessions (broker, sub, sid) VALUES (?, ?, ?)", (broker, _key(subject), sid))
    payload = {"sub": subject, "b": broker, "sid": sid, "iat": int(time.time()), "exp": int(time.time()) + TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def inspect(token: str, broker: Optional[str] = None) -> Tuple[Optional[dict], Optional[str]]:
    """(payload, None) for a live token, else (None, "invalid" | "other_device")."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):      # constant-time compare
            return None, "invalid"
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None, "invalid"
        if broker and payload.get("b") != broker:           # a Quotex token is useless on the Pocket Option API
            return None, "invalid"
        with _connect() as conn:
            row = conn.execute("SELECT sid FROM sessions WHERE broker = ? AND sub = ?",
                               (payload.get("b"), _key(str(payload.get("sub", ""))))).fetchone()
        if row is None or row[0] != payload.get("sid"):
            return None, "other_device"
        return payload, None
    except Exception:
        return None, "invalid"


def verify(token: str, broker: Optional[str] = None) -> Optional[dict]:
    return inspect(token, broker)[0]


def make_dependency(broker: str):
    """FastAPI dependency factory: 401 unless a valid token for this broker is presented."""
    async def require_session(authorization: Optional[str] = Header(None)) -> Optional[dict]:
        if not ENFORCE:
            return None
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Sign in required", headers={"WWW-Authenticate": "Bearer"})
        payload, reason = inspect(authorization[7:].strip(), broker)
        if payload is None:
            detail = OTHER_DEVICE if reason == "other_device" else "Session expired, please sign in again"
            raise HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})
        return payload
    return require_session
