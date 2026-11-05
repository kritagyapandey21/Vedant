"""Adapter that lets AsyncWebSocketClient use the tested Chrome transport from the quotex-api project.

Quotex sits behind Cloudflare, which answers every non-browser WebSocket upgrade with HTTP 403 or a challenge page
(Python websockets, curl_cffi, Playwright's Chromium ...). Only a real Chrome passes, so the socket is opened by
JavaScript inside a Chrome page and bridged to Python. That bridge is `browser_websocket.py` (vendored unchanged
from quotex-api, see tackle.md sections 2.4 and 2.5 there). It is synchronous and thread based; this module only
wraps it in the small asyncio interface of a `websockets` connection (send / recv / close / closed).

Note on frames: the bridge re-assembles Socket.IO binary events (`451-["name",{placeholder}]` + blob) into ordinary
`42["name", <payload>]` text frames, so the client receives text only.

Also here: refresh_session(), which renews the session token through Chrome using the cookies saved in session.json.
"""

import asyncio
import os
import sys
import tempfile
import threading
import shutil
import time
from typing import Any, Dict, Optional

from loguru import logger
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from . import browser_websocket as bridge

_CLOSED = object()   # sentinel in the receive queue


def build_ssid(token: str, is_demo: bool) -> str:
    """The Socket.IO authorization frame for a session token."""
    return f'42["authorization",{{"session":"{token}","isDemo":{1 if is_demo else 0},"tournamentId":0}}]'


class BrowserWebSocket:
    """The slice of a `websockets` connection that AsyncWebSocketClient needs, backed by the vendored Chrome bridge."""

    def __init__(self, transport: "bridge.BrowserWebSocketTransport", loop: asyncio.AbstractEventLoop) -> None:
        self._t = transport
        self._loop = loop
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self._closed = False
        self._reader: Optional[threading.Thread] = None
        self.close_reason: Optional[str] = None

    @classmethod
    async def connect(cls, url: str) -> "BrowserWebSocket":
        loop = asyncio.get_running_loop()
        transport = bridge.BrowserWebSocketTransport(url)
        ws = cls(transport, loop)
        # The transport blocks for ~10-20 s while Chrome starts, so run it off the event loop.
        # (No on_message callback: the bridge already queues every frame; we read that queue below, once.)
        if not await loop.run_in_executor(None, transport.connect):
            raise RuntimeError("Chrome could not open the Quotex WebSocket (blocked by Cloudflare, or Chrome failed to start)")
        ws._reader = threading.Thread(target=ws._read_loop, daemon=True, name="QuotexBrowserReader")
        ws._reader.start()
        return ws

    @property
    def closed(self) -> bool:
        return self._closed

    def _push(self, item: Any) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def _read_loop(self) -> None:
        """Move frames from the bridge's thread-safe queue into the asyncio queue, in order."""
        while True:
            frame = self._t.recv(timeout=0.5)
            if frame is not None:
                self._push(frame)
                continue
            if not self._t.is_connected():           # bridge stopped and its queue is drained
                self._finish("browser WebSocket closed")
                return

    def _finish(self, reason: Optional[str]) -> None:
        if not self._closed:
            self._closed = True
            self.close_reason = reason
            self._push(_CLOSED)

    async def recv(self) -> str:
        item = await self._queue.get()
        if item is _CLOSED:
            self._queue.put_nowait(_CLOSED)          # every later recv() fails the same way
            raise ConnectionClosedOK(None, None)
        return item

    async def send(self, message: str) -> None:
        if self._closed:
            raise ConnectionClosedError(None, None)
        ok = await self._loop.run_in_executor(None, self._t.send, message)
        if not ok:
            self._finish("send failed")
            raise ConnectionClosedError(None, None)

    async def close(self) -> None:
        await self._loop.run_in_executor(None, self._t.close)
        self._finish("closed locally")


# ---------------------------------------------------------------------------------- session renewal
def _new_driver():
    """A throwaway Chrome set up exactly like the bridge's."""
    import undetected_chromedriver as uc

    profile = tempfile.mkdtemp(prefix="quotex_refresh_")
    options = uc.ChromeOptions()
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--window-size=1366,768", f"--user-data-dir={profile}"):
        options.add_argument(arg)
    headless = os.getenv("QUOTEX_BROWSER_HEADLESS")
    if headless == "1" or (headless is None and sys.platform.startswith("linux") and not os.getenv("DISPLAY")):
        options.add_argument("--headless=new")
    kwargs: Dict[str, Any] = {}
    chrome = os.path.join(bridge._BROWSER_DIR, "chrome-linux64", "chrome")
    driver_bin = os.path.join(bridge._BROWSER_DIR, "chromedriver-linux64", "chromedriver")
    if os.path.exists(chrome) and os.path.exists(driver_bin):
        kwargs = {"browser_executable_path": chrome, "driver_executable_path": driver_bin}
    return uc.Chrome(options=options, use_subprocess=True, **kwargs), profile


CHALLENGE_MARKERS = ("just a moment", "attention required", "access denied", "verify you are human")


def _wait_clear(driver, seconds: float = 40) -> bool:
    """Wait for Cloudflare's check page to go away (the login tool does the same)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(m in (driver.page_source or "").lower() for m in CHALLENGE_MARKERS):
            return True
        time.sleep(1)
    return False


def _page_hint(driver) -> str:
    try:
        text = driver.execute_script("return (document.body && document.body.innerText || '').replace(/\\s+/g,' ').slice(0,200)")
    except Exception:
        text = ""
    return f"url={driver.current_url!r} title={driver.title!r} text={text!r}"


def _refresh_blocking(cookies: str, is_demo: bool, lang: str) -> Optional[Dict[str, Any]]:
    """Open the trade page logged in with the saved account cookies and read the current session token from it.

    Only account cookies are injected (never Cloudflare's: those belong to the browser that earned them). The Chrome is
    closed again right away, so no logged-in page stays open to hold a second socket on the same session.
    Every way of failing says why in the log, so a refused renewal can be understood.
    """
    from urllib.parse import urlparse
    from .login import _cookies_string_to_dict, _extract_token_from_trade

    jar = {k: v for k, v in _cookies_string_to_dict(cookies).items()
           if not k.lower().startswith(("cf_", "__cf", "_ga", "__vid"))}
    if not jar:
        logger.warning("Quotex refresh: no account cookies are saved, a new login is needed")
        return None
    origin = urlparse(bridge.PAGE_URL)
    base = f"{origin.scheme}://{origin.netloc}"
    driver, profile = _new_driver()
    try:
        driver.set_script_timeout(30)
        driver.get(bridge.PAGE_URL)
        if not _wait_clear(driver):                     # let Cloudflare's check run and finish
            logger.warning(f"Quotex refresh: Cloudflare check did not clear ({_page_hint(driver)})")
            return None
        for name, value in jar.items():
            driver.execute_cdp_cmd("Network.setCookie", {"name": name, "value": value, "url": base + "/",
                                                         "path": "/", "secure": origin.scheme == "https"})
        driver.get(f"{base}/{lang}/{'demo-trade' if is_demo else 'trade'}")
        if not _wait_clear(driver):
            logger.warning(f"Quotex refresh: Cloudflare check did not clear on the trade page ({_page_hint(driver)})")
            return None
        token = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if "sign-in" in (driver.current_url or ""):
                logger.warning("Quotex refresh: the saved cookies are no longer logged in, a new login is needed")
                return None
            token = _extract_token_from_trade(driver.page_source)
            if not token:
                try:
                    token = driver.execute_script("return (window.settings && window.settings.token) || null")
                except Exception:
                    token = None
            if token:
                break
            time.sleep(1)
        if not token:
            logger.warning(f"Quotex refresh: logged in but no session token on the page ({_page_hint(driver)})")
            return None
        fresh = {c["name"]: c["value"] for c in driver.get_cookies() if not c["name"].lower().startswith(("cf_", "__cf"))}
        return {"token": token, "cookies": "; ".join(f"{k}={v}" for k, v in fresh.items()),
                "user_agent": driver.execute_script("return navigator.userAgent")}
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)


async def refresh_session(cookies: str, is_demo: bool, lang: str = "en") -> Optional[Dict[str, Any]]:
    """Renew the session token through Chrome. Returns {token, cookies, user_agent} or None if it did not work."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _refresh_blocking, cookies, is_demo, lang)
    except Exception as e:
        logger.error(f"Quotex session refresh failed: {e}")
        return None
