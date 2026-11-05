"""Quotex data feed: keeps a fresh cache of 1-minute candles for every open, high-payout pair.

Data only - this module never places orders.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import alerts
from config import (
    QX_CANDLES, QX_DATA_DIR, QX_EMAIL, QX_IS_DEMO, QX_LIB_PATH, QX_MIN_PAYOUT,
    QX_PASSWORD, QX_ASSET_TYPES, QX_REAL_MARKET_ONLY, QX_REFRESH_SECONDS,
)

logger = logging.getLogger(__name__)

TF = 60                       # 1-minute candles
FETCH_CONCURRENCY = 6
FETCH_TIMEOUT = 20
MAX_FAILED_CYCLES = 2         # failed cycles before the connection is rebuilt
RELOGIN_MIN_INTERVAL = 600    # never run the browser login more than once per 10 min
# "browser": the server cannot log in or open the socket with plain Python (Cloudflare answers 403), so the feed
# uses the saved session.json and a real Chrome for the socket (api_quotex/browser_ws.py). Default "direct" = as before.
BROWSER_MODE = os.environ.get("QUOTEX_TRANSPORT", "direct").strip().lower() == "browser"
AUTO_LOGIN_MIN_INTERVAL = 3600   # at most one automatic full login per hour
AUTO_LOGIN_MAX_FAILURES = 3      # then wait for a login by hand
LOGIN_TOOL_TIMEOUT = 180
LOGIN_NEEDS_CODE = 2             # exit status of browser_login.py --no-code


def _run_login_tool(session_file) -> tuple:
    """Run the login tool in its own process, as a person would. Returns (exit code, end of its output)."""
    cmd = [sys.executable, str(Path(QX_LIB_PATH) / "tools" / "browser_login.py"), "--out", str(session_file),
           "--no-code", "--timeout", str(LOGIN_TOOL_TIMEOUT)]
    if not QX_IS_DEMO:
        cmd.append("--live")
    # Own process group: if it hangs, its Chrome has to go too. The password comes from the environment, never the command line.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=LOGIN_TOOL_TIMEOUT + 60)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        output, _ = proc.communicate()
        code = -1
    tail = (output or "").strip()[-500:]
    for secret in (QX_EMAIL, QX_PASSWORD):
        if secret:
            tail = tail.replace(secret, "***")
    return code, tail


def _prepare_environment() -> None:
    """api_quotex reads/writes sessions/ and config.json relative to the CWD."""
    QX_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(QX_DATA_DIR)
    if QX_LIB_PATH and QX_LIB_PATH not in sys.path:
        sys.path.insert(0, QX_LIB_PATH)
    sessions = QX_DATA_DIR / "sessions"
    sessions.mkdir(exist_ok=True)
    cfg = sessions / "config.json"
    if QX_EMAIL and QX_PASSWORD and not cfg.exists():
        cfg.write_text(json.dumps({"email": QX_EMAIL, "password": QX_PASSWORD, "lang": "en", "user_data_dir": "."}))


class TickAggregator:
    """Turns quote_stream ticks [asset, ts, price, ...] into closed 1-minute candles."""

    def __init__(self) -> None:
        self.buckets: Dict[str, list] = {}      # asset -> [minute, open, high, low, close]
        self.skip_minute: Dict[str, int] = {}   # first (partial) minute seen per asset
        self.closed: Dict[str, Dict[int, list]] = {}   # asset -> {minute: [ts, o, h, l, c]}
        self.price: Dict[str, float] = {}

    def reset(self) -> None:
        self.buckets.clear()
        self.skip_minute.clear()

    def on_quotes(self, data) -> None:
        if not isinstance(data, list):
            return
        for q in data:
            try:
                asset, ts, price = q[0], float(q[1]), float(q[2])
            except (TypeError, ValueError, IndexError):
                continue
            minute = int(ts) // TF * TF
            self.price[asset] = price
            b = self.buckets.get(asset)
            if b is None:
                self.skip_minute[asset] = minute          # joined mid-minute: incomplete
                self.buckets[asset] = [minute, price, price, price, price]
            elif minute == b[0]:
                b[2] = max(b[2], price)
                b[3] = min(b[3], price)
                b[4] = price
            elif minute > b[0]:
                if b[0] != self.skip_minute.get(asset):
                    rows = self.closed.setdefault(asset, {})
                    rows[b[0]] = [b[0], b[1], b[2], b[3], b[4]]
                    if len(rows) > 400:
                        del rows[min(rows)]
                self.buckets[asset] = [minute, price, price, price, price]


class QuotexFeed:
    def __init__(self) -> None:
        self.candles: Dict[str, List[list]] = {}     # symbol -> [[ts, open, high, low, close], ...] closed + forming
        self.latest_price: Dict[str, float] = {}
        self.agg = TickAggregator()
        self.followed: set = set()
        self.names: Dict[str, str] = {}              # symbol -> display name
        self.payouts: Dict[str, int] = {}
        self.updated_at: Optional[datetime] = None
        self.connected = False
        self.last_error = ""
        self._last_login = 0.0
        self._dead_session_mtime: Optional[float] = None   # session.json that was rejected and could not be renewed
        self._wake: Optional[asyncio.Event] = None
        self._fail_streak = 0

    def request_reconnect(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------------ session
    def _session_file(self):
        return QX_DATA_DIR / "sessions" / "session.json"

    def _session_mtime(self) -> Optional[float]:
        try:
            return self._session_file().stat().st_mtime
        except OSError:
            return None

    def _auto_login_state(self) -> dict:
        """Kept on disk so a service restart cannot get around the limits. A login by hand starts the count again."""
        try:
            state = json.loads(self._auto_login_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if state.get("session_mtime") == self._session_mtime() else {}

    def _auto_login_file(self):
        return QX_DATA_DIR / "sessions" / "auto_login.json"

    async def _auto_login(self) -> bool:
        """Full login with QX_EMAIL / QX_PASSWORD, for when the saved cookies can no longer renew the session.

        Each try costs a Chrome and too many can get the account locked, so they are limited. A Quotex code request
        needs a person, so that stops the tries until someone logs in by hand.
        """
        if not (QX_EMAIL and QX_PASSWORD):
            return False
        state = self._auto_login_state()
        failures = state.get("failures", 0)
        if state.get("needs_code") or failures >= AUTO_LOGIN_MAX_FAILURES:
            return False
        if time.time() - state.get("at", 0) < AUTO_LOGIN_MIN_INTERVAL:
            return False

        def save(**values) -> None:
            self._auto_login_file().write_text(json.dumps(values), encoding="utf-8")

        started = time.time()
        save(at=started, failures=failures, session_mtime=self._session_mtime())
        logger.info("Quotex auto login: starting (attempt %d of %d)", failures + 1, AUTO_LOGIN_MAX_FAILURES)
        code, output = await asyncio.to_thread(_run_login_tool, self._session_file())
        logger.info("Quotex auto login: tool exited with %s. %s", code, output)
        self._last_login = time.time()

        if code == 0:
            self._dead_session_mtime = None
            save(at=started, failures=0, session_mtime=self._session_mtime())
            await alerts.send("logged in again automatically after the session stopped working.")
            return True
        if code == LOGIN_NEEDS_CODE:
            save(at=started, failures=failures, needs_code=True, session_mtime=self._session_mtime())
            await alerts.send("Quotex is asking for an emailed code, so automatic login cannot finish. Run "
                              "quotex_lib/tools/browser_login.py on the server.")
            return False
        failures += 1
        save(at=started, failures=failures, session_mtime=self._session_mtime())
        if failures >= AUTO_LOGIN_MAX_FAILURES:
            await alerts.send(f"automatic login failed {failures} times and has stopped trying. Run "
                              "quotex_lib/tools/browser_login.py on the server.")
        return False

    async def _login_from_session(self, force: bool) -> str:
        """Browser mode: use the saved session; on force, renew it through Chrome with the saved cookies."""
        from api_quotex.browser_ws import build_ssid, refresh_session
        path = self._session_file()
        # A session Quotex rejected that could not be renewed is not tried again, and no Chrome is started for it,
        # until a new login rewrites session.json (every attempt costs a Chrome, ~1 GB, and used to end in an OOM kill).
        if self._dead_session_mtime is not None and self._session_mtime() == self._dead_session_mtime:
            if not await self._auto_login():
                raise RuntimeError("the saved Quotex session was rejected and could not be renewed - waiting for a new login "
                                   "(run quotex_lib/tools/browser_login.py on the server)")
            force = False
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if force:
            self._last_login = time.time()
            fresh = await refresh_session(saved["cookies"], QX_IS_DEMO) if saved.get("cookies") else None
            if not fresh or not fresh.get("token"):
                self._dead_session_mtime = self._session_mtime()
                if not await self._auto_login():
                    raise RuntimeError("Quotex rejected the saved session and it could not be renewed - run "
                                       "quotex_lib/tools/browser_login.py on the server to log in again")
                saved = json.loads(path.read_text(encoding="utf-8"))
            else:
                saved.update({k: v for k, v in fresh.items() if v})
                saved["ssid"] = build_ssid(saved["token"], QX_IS_DEMO)
                saved["is_demo"] = QX_IS_DEMO
                path.write_text(json.dumps(saved, indent=4), encoding="utf-8")
                os.chmod(path, 0o600)
                self._dead_session_mtime = None
                logger.info("Quotex session renewed through Chrome")
        if not saved.get("ssid"):
            raise RuntimeError(f"no Quotex session found at {path} - run quotex_lib/tools/browser_login.py on the server")
        return saved["ssid"]

    async def _login(self, force: bool = False) -> str:
        """Return an SSID. force=True discards the saved session first.

        api_quotex's validate_ssid() only checks the handshake, so an expired session still "validates";
        the only reliable test is a real connect, hence the explicit force path.
        """
        if BROWSER_MODE:
            return await self._login_from_session(force)
        from api_quotex.login import get_ssid
        session_file = QX_DATA_DIR / "sessions" / "session.json"
        if force and session_file.exists():
            session_file.replace(session_file.with_suffix(".json.old"))
            self._last_login = time.time()

        def run() -> tuple:
            # Own thread + loop: the browser login blocks for tens of seconds and must not freeze the API.
            return asyncio.run(get_ssid(is_demo=QX_IS_DEMO))

        ok, session = await asyncio.to_thread(run)
        if not ok or not session.get("ssid"):
            raise RuntimeError("Quotex login failed (check QX_EMAIL / QX_PASSWORD, captcha or PIN prompt)")
        return session["ssid"]

    async def _connect(self):
        from api_quotex.client import AsyncQuotexClient
        for attempt in (0, 1):
            force = attempt == 1
            if force and time.time() - self._last_login < RELOGIN_MIN_INTERVAL:
                raise RuntimeError("session rejected; re-login throttled")
            ssid = await self._login(force=force)
            # auto_reconnect=False: this feed is the only thing allowed to reconnect (a second, library-side
            # reconnect loop on the same session makes Quotex kick both connections)
            client = AsyncQuotexClient(ssid=ssid, is_demo=QX_IS_DEMO, auto_reconnect=False)
            if await client.connect():
                return client
            logger.warning("Quotex connect failed (attempt %d) - %s", attempt + 1, "refreshing session" if not force else "giving up")
            try:
                await client.disconnect()
            except Exception:
                pass
        raise RuntimeError("could not connect to Quotex")

    # ------------------------------------------------------------------ data
    async def _refresh(self, client) -> int:
        assets = await asyncio.wait_for(client.get_available_assets(), timeout=FETCH_TIMEOUT)
        eligible = {
            sym: info for sym, info in assets.items()
            if info.get("is_open")
            and (info.get("payout") or 0) >= QX_MIN_PAYOUT
            and TF in (info.get("available_timeframes") or [])
            and info.get("type") in QX_ASSET_TYPES
            and not (QX_REAL_MARKET_ONLY and "otc" in f"{sym} {info.get('name', '')}".lower())
        }
        logger.info("Quotex feed: %d assets, %d eligible (payout>=%d%%, types=%s, real market only=%s)",
                    len(assets), len(eligible), QX_MIN_PAYOUT, ",".join(sorted(QX_ASSET_TYPES)), QX_REAL_MARKET_ONLY)

        for sym in list(self.candles):
            if sym not in eligible:
                self.candles.pop(sym, None)
                self.latest_price.pop(sym, None)
        self.payouts = {s: i.get("payout", 0) for s, i in eligible.items()}

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        ok = 0

        async def fetch(sym: str) -> None:
            nonlocal ok
            async with sem:
                try:
                    cs = await asyncio.wait_for(client.get_fresh_candles(sym, TF, QX_CANDLES), timeout=FETCH_TIMEOUT)
                except Exception as e:
                    logger.warning("Quotex feed: %s fetch failed: %s", sym, e)
                    return
            # newest history candle was still forming when the snapshot was taken -> drop it
            rows = sorted(([c.timestamp.timestamp(), c.open, c.high, c.low, c.close] for c in cs[:-1]), key=lambda r: r[0])
            if rows:
                self.candles[sym] = rows[-200:]
                self.latest_price.setdefault(sym, rows[-1][4])
                self.names[sym] = eligible[sym].get("name") or sym
                ok += 1

        await asyncio.gather(*(fetch(s) for s in eligible))

        # live ticks: Quotex history lags ~5 min, so recent candles are built from the tick stream
        for sym in [s for s in eligible if s not in self.followed]:
            await client.send_message(f'42["instruments/follow","{sym}"]')
            await client.send_message(f'42["instruments/update",{{"asset":"{sym}","period":{TF}}}]')
            self.followed.add(sym)
            await asyncio.sleep(0.02)
        return ok if eligible else -1       # -1: nothing eligible (market closed) - not a failure

    def _merged(self, symbol: str) -> List[list]:
        """History candles overlaid with tick-built ones, oldest first, keyed by minute."""
        rows = {int(r[0]): r for r in self.candles.get(symbol, [])}
        for minute, r in self.agg.closed.get(symbol, {}).items():
            rows.setdefault(minute, r)
        return [rows[k] for k in sorted(rows)]

    def closed_rows(self, symbol: str) -> List[list]:
        """Latest gap-free run of fully finished candles (indicators must never straddle a gap)."""
        now = time.time()
        rows = [r for r in self._merged(symbol) if r[0] + TF <= now]
        run: List[list] = []
        for r in reversed(rows):
            if run and run[-1][0] - r[0] != TF:
                break
            run.append(r)
        return run[::-1]

    def closed_closes(self, symbol: str) -> List[float]:
        return [r[4] for r in self.closed_rows(symbol)]

    def price(self, symbol: str) -> Optional[float]:
        return self.agg.price.get(symbol) or self.latest_price.get(symbol)

    def last_closed_age(self, symbol: str) -> float:
        rows = self.closed_rows(symbol)
        return time.time() - (rows[-1][0] + TF) if rows else 1e9

    def chart(self, symbol: str, n: int = 60) -> List[dict]:
        return [{"time": int(r[0]), "close": r[4]} for r in self.closed_rows(symbol)[-n:]]

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        _prepare_environment()
        self._wake = asyncio.Event()
        while True:
            self._wake.clear()
            client = None
            try:
                client = await self._connect()
                self.followed.clear()
                self.agg.reset()
                client.add_event_callback("quote_stream", self.agg.on_quotes)
                self.connected = True
                self.last_error = ""
                self._fail_streak = 0
                logger.info("Quotex feed: connected (%s account)", "demo" if QX_IS_DEMO else "live")
                failed = 0
                while True:
                    try:
                        result = await self._refresh(client)
                        if result > 0:
                            self.updated_at = datetime.now(timezone.utc)
                            failed = 0
                            logger.info("Quotex feed: cache updated - %d pairs", len(self.candles))
                        elif result == 0:
                            raise RuntimeError("every candle fetch failed")
                        else:
                            self.updated_at = datetime.now(timezone.utc)   # market closed: connection is fine
                            logger.info("Quotex feed: no pair currently meets the payout/type filter")
                    except Exception as e:
                        failed += 1
                        self.last_error = str(e)
                        logger.warning("Quotex feed: cycle failed (%d/%d): %s", failed, MAX_FAILED_CYCLES, e)
                        if failed >= MAX_FAILED_CYCLES or not client.is_connected:
                            raise
                    # wake a few seconds after the minute rolls so the newest closed candle is available
                    now = time.time()
                    nxt = (now // QX_REFRESH_SECONDS + 1) * QX_REFRESH_SECONDS + 3
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=max(5, nxt - now))
                        raise RuntimeError("reconnect requested")
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                self._fail_streak += 1
                delay = min(30 * 2 ** (self._fail_streak - 1), 600)   # 30s, 60s, 2m ... capped at 10 min
                logger.error("Quotex feed: connection lost (%s) - retrying in %ds", e, delay)
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            await asyncio.sleep(delay if self._fail_streak else 30)
