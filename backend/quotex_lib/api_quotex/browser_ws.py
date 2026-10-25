"""WebSocket transport that runs inside a real Chrome (undetected-chromedriver).

Quotex sits behind Cloudflare, which answers every non-browser WebSocket upgrade (Python websockets, curl_cffi,
Playwright's Chromium ...) with HTTP 403 or a challenge page. A real Chrome passes, so the socket is opened by
JavaScript in a Chrome page and bridged to Python. Findings: tackle.md (sections 2.4 and 2.5) in the quotex-api repo, https://github.com/kritagyapandey21/quotex-api

BrowserWebSocket mimics the small part of the `websockets` connection that AsyncWebSocketClient uses
(send / recv / close / closed). Text frames are returned as str and binary frames as the raw bytes, so all the
existing Socket.IO parsing (451- placeholder + "\\x04<json>" frame) keeps working unchanged.

Environment:
  QUOTEX_BROWSER_DIR       folder with chrome-linux64/chrome and chromedriver-linux64/chromedriver
                           (installed by deploy/install_quotex_browser.sh); otherwise the system Chrome is used
  QUOTEX_BROWSER_HEADLESS  "1" forces headless Chrome (more likely to be blocked). Default: headless only on Linux
                           without a DISPLAY; on a server run under `xvfb-run -a`
  QUOTEX_PAGE_URL          page Chrome opens first (default https://quotex.com/en/trade)
"""

import asyncio
import atexit
import base64
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Union

from loguru import logger
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

PAGE_URL = os.environ.get("QUOTEX_PAGE_URL", "https://quotex.com/en/trade")
CHALLENGE_MARKERS = ("just a moment", "attention required", "access denied", "verify you are human")
POLL_INTERVAL = 0.05

# Opens the socket and buffers every frame: ['s', text] or ['b', base64 of the raw binary frame].
# The page also sends the Engine.IO v3 client ping ("2") every 20 s: the server drops a connection that is silent
# for ~30 s, and doing it in the page keeps it alive even while Python is busy.
_BRIDGE_JS = """
const url = arguments[0];
const old = window.__qx;
if (old) { try { clearInterval(old.pinger); old.ws.close(); } catch (e) {} }
const qx = {q: [], ws: null, state: 'connecting', err: null, pinger: null};
window.__qx = qx;
const b64 = (buf) => {
  const u = new Uint8Array(buf); let s = '';
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s);
};
const ws = new WebSocket(url);
ws.binaryType = 'arraybuffer';
qx.ws = ws;
ws.onopen = () => {
  qx.state = 'open';
  qx.pinger = setInterval(() => { if (ws.readyState === 1) ws.send('2'); }, 20000);
};
ws.onclose = (e) => { qx.state = 'closed'; qx.err = 'closed ' + e.code; clearInterval(qx.pinger); };
ws.onerror = () => { qx.err = 'websocket error'; };
ws.onmessage = (m) => { qx.q.push(typeof m.data === 'string' ? ['s', m.data] : ['b', b64(m.data)]); };
"""
_DRAIN_JS = "const q = window.__qx.q; window.__qx.q = []; return [window.__qx.state, window.__qx.err, q];"
_STATE_JS = "return window.__qx ? [window.__qx.state, window.__qx.err] : ['none', null];"
_CLOSE_JS = "if (window.__qx && window.__qx.ws) { clearInterval(window.__qx.pinger); try { window.__qx.ws.close(); } catch (e) {} }"
_SEND_JS = "window.__qx.ws.send(arguments[0]);"

_CLOSED = object()   # sentinel in the receive queue


def build_ssid(token: str, is_demo: bool) -> str:
    """The Socket.IO authorization frame for a session token."""
    return f'42["authorization",{{"session":"{token}","isDemo":{1 if is_demo else 0},"tournamentId":0}}]'


class BrowserSession:
    """One Chrome per process, shared by every connection attempt (starting it takes ~10 s)."""

    def __init__(self) -> None:
        self.driver = None
        self._profile_dir: Optional[str] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qx-chrome")  # webdriver is not thread-safe
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------------ run on the chrome thread
    async def run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, lambda: fn(*args))

    async def ensure_started(self) -> None:
        await self.run(self._start)

    def _alive(self) -> bool:
        if self.driver is None:
            return False
        try:
            self.driver.execute_script("return 1")
            return True
        except Exception:
            return False

    def _start(self) -> None:
        with self._start_lock:
            if self._alive():
                return
            self._quit()
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            self._profile_dir = tempfile.mkdtemp(prefix="quotex_browser_")   # throwaway profile per Chrome start
            for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-first-run",
                        "--mute-audio", "--window-size=1366,768", f"--user-data-dir={self._profile_dir}"):
                options.add_argument(arg)
            headless = os.environ.get("QUOTEX_BROWSER_HEADLESS")
            if headless == "1" or (headless is None and sys.platform.startswith("linux") and not os.environ.get("DISPLAY")):
                options.add_argument("--headless=new")

            kwargs: Dict[str, Any] = {}
            browser_dir = os.environ.get("QUOTEX_BROWSER_DIR", "")
            chrome = os.path.join(browser_dir, "chrome-linux64", "chrome")
            driver_bin = os.path.join(browser_dir, "chromedriver-linux64", "chromedriver")
            if browser_dir and os.path.exists(chrome) and os.path.exists(driver_bin):
                kwargs = {"browser_executable_path": chrome, "driver_executable_path": driver_bin}

            logger.info("Starting Chrome for the Quotex connection ...")
            self.driver = uc.Chrome(options=options, use_subprocess=True, **kwargs)
            self.driver.set_script_timeout(30)
            self._open_page(PAGE_URL)

    def _open_page(self, url: str, settle: float = 5.0, wait_challenge: float = 30.0) -> None:
        """Load a page and give Cloudflare's check time to run; raises if it never lets us through."""
        self.driver.get(url)
        time.sleep(settle)
        deadline = time.time() + wait_challenge
        while True:
            source = (self.driver.page_source or "").lower()
            if not any(marker in source for marker in CHALLENGE_MARKERS):
                return
            if time.time() > deadline:
                raise RuntimeError("Cloudflare blocked the browser page (this server address is rejected)")
            time.sleep(1)

    def _quit(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    async def shutdown(self) -> None:
        await self.run(self._quit)

    # ------------------------------------------------------------------ socket primitives (chrome thread)
    def _open_socket(self, url: str, timeout: float) -> None:
        self.driver.execute_script(_BRIDGE_JS, url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state, err = self.driver.execute_script(_STATE_JS)
            if state == "open":
                return
            if state == "closed" or err:
                raise RuntimeError(f"browser WebSocket failed: {err or state}")
            time.sleep(0.1)
        raise RuntimeError("browser WebSocket did not open in time")

    def _drain(self):
        return self.driver.execute_script(_DRAIN_JS)

    def _send(self, message: str) -> None:
        self.driver.execute_script(_SEND_JS, message)

    def _close_socket(self) -> None:
        if self.driver is not None:
            try:
                self.driver.execute_script(_CLOSE_JS)
            except Exception:
                pass

    # ------------------------------------------------------------------ session refresh
    def _refresh(self, cookies: str, is_demo: bool, lang: str = "en") -> Optional[Dict[str, Any]]:
        """Open the trade page logged in with saved cookies and read the current session token from it.

        Only the account cookies are injected (never Cloudflare's: those are tied to the browser that earned them).
        Afterwards they are removed again and the neutral page is reloaded, so this Chrome does not stay logged in
        and open a second socket on the same session, which would make Quotex drop ours.
        """
        from .login import _cookies_string_to_dict, _extract_token_from_trade

        jar = {k: v for k, v in _cookies_string_to_dict(cookies).items()
               if not k.lower().startswith(("cf_", "__cf", "_ga", "__vid"))}
        if not jar:
            return None
        for name, value in jar.items():
            self.driver.execute_cdp_cmd("Network.setCookie", {"name": name, "value": value, "url": "https://quotex.com/",
                                                              "path": "/", "secure": True})
        target = f"https://quotex.com/{lang}/{'demo-trade' if is_demo else 'trade'}"
        self._open_page(target, settle=6.0)
        if "sign-in" in (self.driver.current_url or ""):
            logger.warning("Quotex refresh: the saved cookies are no longer logged in")
            result = None
        else:
            token = _extract_token_from_trade(self.driver.page_source)
            fresh = {c["name"]: c["value"] for c in self.driver.get_cookies()
                     if not c["name"].lower().startswith(("cf_", "__cf"))}
            result = {"token": token, "cookies": "; ".join(f"{k}={v}" for k, v in fresh.items()),
                      "user_agent": self.driver.execute_script("return navigator.userAgent")} if token else None
        for cookie in self.driver.get_cookies():     # leave the browser logged out
            if cookie["name"] in jar or cookie["name"].lower().startswith(("laravel_session", "remember_web", "xsrf")):
                try:
                    self.driver.execute_cdp_cmd("Network.deleteCookies", {"name": cookie["name"], "url": "https://quotex.com/"})
                except Exception:
                    pass
        try:
            self._open_page(PAGE_URL, settle=3.0)
        except Exception:
            pass
        return result

    async def refresh(self, cookies: str, is_demo: bool) -> Optional[Dict[str, Any]]:
        await self.ensure_started()
        return await self.run(self._refresh, cookies, is_demo)


_session: Optional[BrowserSession] = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
        atexit.register(lambda: _session and _session._quit())
    return _session


class BrowserWebSocket:
    """The slice of a `websockets` connection that AsyncWebSocketClient needs, backed by a Chrome page."""

    def __init__(self, session: BrowserSession) -> None:
        self._s = session
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self._closed = False
        self._pump_task: Optional[asyncio.Task] = None
        self.close_reason: Optional[str] = None

    @classmethod
    async def connect(cls, url: str, timeout: float = 20.0) -> "BrowserWebSocket":
        session = get_session()
        await session.ensure_started()
        ws = cls(session)
        await session.run(session._open_socket, url, timeout)
        ws._pump_task = asyncio.create_task(ws._pump())
        return ws

    @property
    def closed(self) -> bool:
        return self._closed

    def _finish(self, reason: Optional[str]) -> None:
        if not self._closed:
            self._closed = True
            self.close_reason = reason
        self._queue.put_nowait(_CLOSED)

    async def _pump(self) -> None:
        while not self._closed:
            try:
                state, err, frames = await self._s.run(self._s._drain)
            except Exception as e:
                logger.error(f"Browser poll error: {e}")
                self._finish(f"browser error: {e}")
                return
            for kind, data in frames:
                self._queue.put_nowait(data if kind == "s" else base64.b64decode(data))
            if state == "closed":
                logger.error(f"Browser WebSocket closed: {err}")
                self._finish(err)
                return
            await asyncio.sleep(POLL_INTERVAL)

    async def recv(self) -> Union[str, bytes]:
        item = await self._queue.get()
        if item is _CLOSED:
            self._queue.put_nowait(_CLOSED)          # every later recv() fails the same way
            raise ConnectionClosedOK(None, None)
        return item

    async def send(self, message: str) -> None:
        if self._closed:
            raise ConnectionClosedError(None, None)
        try:
            await self._s.run(self._s._send, message)
        except Exception as e:
            self._finish(f"send failed: {e}")
            raise ConnectionClosedError(None, None) from e

    async def close(self) -> None:
        if not self._closed:
            try:
                await self._s.run(self._s._close_socket)
            except Exception:
                pass
        self._finish(self.close_reason or "closed locally")
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
