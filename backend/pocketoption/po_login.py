"""Pocket Option login in the server's own Chrome: signs in and captures the 42["auth",...] token the data feed needs.

Why the browser frame: the feed needs the whole auth frame (session, uid, isDemo, platform), which only the trading page
sends. The lo_uid cookie that po_auth.py returns is not enough.

Test run, nothing is saved (asks for the email and password, prints only the account id and the token length):

    cd /opt/tradingsignal/backend/pocketoption && sudo -u tsignal env QUOTEX_BROWSER_DIR=/opt/tradingsignal/browsers/cft \\
        xvfb-run -a /opt/tradingsignal/venv/bin/python po_login.py

Renewal without a password, from cookies saved by --save-cookies or copied from a logged-in browser (the value of
the `cookie:` request header in DevTools, or a Cookie-Editor JSON export):

    ... po_login.py --renew /path/po_cookies.json --out /path/token.txt

Exit status: 0 token captured, 1 failed, 2 Pocket Option wants a person (captcha or emailed code),
3 the saved cookies are no longer logged in.
PO_EMAIL / PO_PASSWORD are read from the environment if set, never from the command line.
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "quotex_lib"))

BASE = "https://pocketoption.com/en"
LOGIN_URL = f"{BASE}/login/"
TRADE_URL = {True: f"{BASE}/cabinet/demo-quick-high-low/", False: f"{BASE}/cabinet/quick-high-low/"}
PERSON_NEEDED = 2
COOKIES_EXPIRED = 3
# Marks a browser Pocket Option has already trusted. Copied alone: the login cookies would log in as their own account.
TRUST_COOKIES = ("no-login-captcha",)
HUMAN_TEXT = ("verify you are human", "confirmation code", "verification code", "enter the code")
CAPTCHA_FRAMES = ("recaptcha", "hcaptcha", "turnstile")
CHALLENGE_MARKERS = ("just a moment", "attention required", "access denied")
PAGE_LOAD_TIMEOUT = 45   # the token is sent once the page's socket opens; the rest of a slow page is not needed

# Runs before the page's own code: keeps a copy of the auth frame the page sends over its socket.
CAPTURE_JS = """
(() => {
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    if (typeof data === 'string' && data.startsWith('42["auth"')) { window.__poAuth = data; }
    return send.call(this, data);
  };
})();
"""


def parse_auth(frame: Optional[str]) -> Optional[dict]:
    """The auth frame's payload, or None if this is not a usable one."""
    match = re.fullmatch(r'42\["auth",(\{.*\})\]', (frame or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    return payload if payload.get("session") else None


def page_hint(driver, secrets) -> str:
    try:
        text = driver.execute_script("return (document.body && document.body.innerText || '').replace(/\\s+/g,' ').slice(0,200)")
    except Exception:
        text = ""
    hint = f"url={driver.current_url!r} title={driver.title!r} text={text!r}"
    for secret in secrets:
        hint = hint.replace(secret, "***")
    return hint


def frame_is_challenge(src: str, width: int, height: int) -> bool:
    """A captcha someone would have to answer. Sites also load invisible ones that show nothing, which do not count."""
    src = src.lower()
    if not any(name in src for name in CAPTCHA_FRAMES) or "size=invisible" in src:
        return False
    return width >= 100 and height >= 50


def wants_person(driver) -> bool:
    """Only what is actually on screen: the words "recaptcha" in the page's code mean nothing by themselves."""
    from selenium.webdriver.common.by import By

    for frame in driver.find_elements(By.CSS_SELECTOR, "iframe"):
        try:
            size = frame.size
            if frame.is_displayed() and frame_is_challenge(frame.get_attribute("src") or "", size["width"], size["height"]):
                return True
        except Exception:
            continue
    try:
        text = (driver.execute_script("return document.body && document.body.innerText || ''") or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in HUMAN_TEXT)


def open_page(driver, url: str) -> None:
    """driver.get that gives up waiting after PAGE_LOAD_TIMEOUT and stops the page instead of hanging."""
    from selenium.common.exceptions import TimeoutException

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    try:
        driver.get(url)
    except TimeoutException:
        print(f"Page still loading after {PAGE_LOAD_TIMEOUT} s, carrying on with what has loaded", flush=True)
        try:
            driver.execute_script("window.stop()")
        except Exception:
            pass


def wait_clear(driver, seconds: float = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(marker in (driver.page_source or "").lower() for marker in CHALLENGE_MARKERS):
            return True
        time.sleep(1)
    return False


def write_private(path: str, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def load_cookies(text: str) -> list:
    """Cookies from a saved JSON list (ours or Cookie-Editor's) or a `cookie:` header value. Cloudflare's own cookies
    are left out: they belong to the browser that earned them."""
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if text.startswith(("[", "{")):
        rows = json.loads(text)
        rows = rows.get("cookies", []) if isinstance(rows, dict) else rows
    else:
        rows = [dict(zip(("name", "value"), part.strip().split("=", 1))) for part in text.split(";") if "=" in part]
    cookies = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name or name.lower().startswith(("cf_", "__cf")):
            continue
        cookie = {"name": name, "value": row.get("value", ""), "path": row.get("path") or "/",
                  "domain": row.get("domain") or ".pocketoption.com", "secure": bool(row.get("secure", True)),
                  "httpOnly": bool(row.get("httpOnly", False))}
        expires = row.get("expirationDate", row.get("expiry", row.get("expires")))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = expires
        cookies.append(cookie)
    return cookies


def wait_for_frame(driver, demo: bool, deadline: float, secrets):
    """Open the trading page and wait for the auth frame it sends. Returns (status, frame)."""
    open_page(driver, TRADE_URL[demo])
    while time.time() < deadline:
        if "/login" in (driver.current_url or ""):
            return COOKIES_EXPIRED, None
        frame = driver.execute_script("return window.__poAuth || null")
        if parse_auth(frame):
            return 0, frame
        if wants_person(driver):
            print("Pocket Option wants a captcha or a code: " + page_hint(driver, secrets))
            return PERSON_NEEDED, None
        time.sleep(1)
    print("Logged in, but the page sent no auth frame: " + page_hint(driver, secrets))
    return 1, None


def saved_cookies(driver) -> str:
    return json.dumps([c for c in driver.get_cookies() if "pocketoption" in (c.get("domain") or "")], indent=1)


def renew(cookie_file: str, demo: bool, timeout: float):
    """Fresh token from saved cookies, no password. Returns (status, frame); refreshes the cookie file on success."""
    from api_quotex.browser_ws import _new_driver

    cookies = load_cookies(open(cookie_file, encoding="utf-8").read())
    if not cookies:
        print(f"No usable cookies in {cookie_file}")
        return 1, None
    print(f"Starting Chrome with {len(cookies)} saved cookies ...", flush=True)
    driver, profile = _new_driver()
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_JS})
        open_page(driver, f"{BASE}/")
        time.sleep(4)
        if not wait_clear(driver):
            print("Cloudflare did not let this Chrome through: " + page_hint(driver, ()))
            return 1, None
        for cookie in cookies:
            driver.execute_cdp_cmd("Network.setCookie", cookie)
        status, frame = wait_for_frame(driver, demo, time.time() + timeout, ())
        if status == COOKIES_EXPIRED:
            print("The saved cookies are no longer logged in: a login is needed.")
        elif status == 0:
            write_private(cookie_file, saved_cookies(driver))
        return status, frame
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)


def login(email: str, password: str, demo: bool, timeout: float, cookie_file: Optional[str] = None,
          trust_file: Optional[str] = None):
    """Returns (status, frame). With cookie_file, also saves the logged-in cookies for renew(). With trust_file, first
    adds only the captcha-trust cookie from it."""
    from selenium.webdriver.common.by import By

    from api_quotex.browser_ws import _new_driver

    secrets = (email, password)
    print("Starting Chrome ...", flush=True)
    driver, profile = _new_driver()
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_JS})
        if trust_file:
            trust = [c for c in load_cookies(open(trust_file, encoding="utf-8").read()) if c["name"] in TRUST_COOKIES]
            print(f"Adding {len(trust)} trust cookie(s) before signing in", flush=True)
            for cookie in trust:
                driver.execute_cdp_cmd("Network.setCookie", cookie)
        open_page(driver, LOGIN_URL)
        time.sleep(4)
        if not wait_clear(driver):
            print("Cloudflare did not let this Chrome through: " + page_hint(driver, secrets))
            return 1, None
        boxes = driver.find_elements(By.CSS_SELECTOR, 'input[type="email"], input[name="email"]')
        secret_boxes = driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
        if not boxes or not secret_boxes:
            print("The sign-in form was not found: " + page_hint(driver, secrets))
            return 1, None
        boxes[0].send_keys(email)
        secret_boxes[0].send_keys(password)
        if wants_person(driver):
            print("The sign-in page shows a captcha: " + page_hint(driver, secrets))
            return PERSON_NEEDED, None
        print("Credentials entered, signing in ...", flush=True)
        next(b for b in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]') if b.is_displayed()).click()

        deadline = time.time() + timeout
        while time.time() < deadline and "/login" in (driver.current_url or ""):
            time.sleep(2)
            if wants_person(driver):
                print("Pocket Option wants a captcha or a code: " + page_hint(driver, secrets))
                return PERSON_NEEDED, None
        if "/login" in (driver.current_url or ""):
            print("Still on the sign-in page: " + page_hint(driver, secrets))
            return 1, None

        print("Logged in, opening the trading page ...", flush=True)
        status, frame = wait_for_frame(driver, demo, deadline, secrets)
        if status == 0 and cookie_file:
            write_private(cookie_file, saved_cookies(driver))
            print(f"Saved the login cookies to {cookie_file}")
        return (1 if status == COOKIES_EXPIRED else status), frame
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)


async def count_working_pairs(frame: str, pairs: int = 5) -> tuple:
    """Fetch candles the way the live feed does. Returns (pairs tried, pairs that returned candles, candles in the last one)."""
    try:
        import BinaryOptionsToolsV2 as botv2
        if hasattr(botv2, "Logger") and not hasattr(botv2.Logger, "warn"):
            botv2.Logger.warn = botv2.Logger.info
    except Exception:
        pass
    from BinaryOptionsToolsV2.config import Config
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

    async with PocketOptionAsync(frame, config=Config(connection_initialization_timeout_secs=20)) as api:
        await asyncio.sleep(5)
        assets = await asyncio.wait_for(api.active_assets(), timeout=20)
        symbols = [a.get("symbol", "") for a in assets
                   if a.get("payout", 0) >= 80 and re.fullmatch(r"[A-Z]{6}(_otc)?", a.get("symbol", ""))][:pairs]
        working = candles = 0
        for symbol in symbols:
            try:
                rows = await asyncio.wait_for(api.get_candles(symbol, 60, 9000), timeout=15)
            except Exception as e:
                print(f"{symbol}: {type(e).__name__}")
                continue
            if rows:
                working += 1
                candles = len(rows)
            await asyncio.sleep(0.5)
    return len(symbols), working, candles


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", dest="demo", action="store_false", help="read the real-money page (default: demo)")
    ap.add_argument("--timeout", type=float, default=120, help="seconds to wait for the login and the token")
    ap.add_argument("--out", help="write the token here (mode 600). Without it nothing is saved")
    ap.add_argument("--check", action="store_true", help="also fetch candles with the token, as the live feed does")
    ap.add_argument("--save-cookies", metavar="FILE", help="after logging in, save the cookies here for --renew")
    ap.add_argument("--renew", metavar="FILE", help="no password: get a fresh token with the cookies saved in FILE")
    ap.add_argument("--trust-cookies", metavar="FILE", help="before a password login, add only the captcha-trust "
                                                           "cookie from FILE")
    ap.set_defaults(demo=True)
    args = ap.parse_args()

    try:
        if args.renew:
            status, frame = renew(args.renew, args.demo, args.timeout)
        else:
            email = os.environ.get("PO_EMAIL") or input("Pocket Option email: ").strip()
            password = os.environ.get("PO_PASSWORD") or getpass.getpass("Pocket Option password: ")
            if not email or not password:
                print("An email and a password are needed.")
                return 1
            status, frame = login(email, password, args.demo, args.timeout, args.save_cookies, args.trust_cookies)
    except Exception as e:
        print(f"Chrome or the page failed ({type(e).__name__}), nothing was saved")
        return 1
    if status != 0:
        return status
    auth = parse_auth(frame)
    print(f"TOKEN OK: account {auth.get('uid')}, isDemo={auth.get('isDemo')}, length {len(frame)}")
    if args.out:
        write_private(args.out, frame)
        print(f"Saved {args.out}")
    if args.check:
        print("Checking candles with this token ...", flush=True)
        tried, working, candles = asyncio.run(count_working_pairs(frame))
        print(f"CANDLES: {working} of {tried} pairs returned data ({candles} candles in the last one)")
        if not working:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
