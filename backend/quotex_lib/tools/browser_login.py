"""One-time interactive Quotex login in the server's own Chrome. Writes session.json for the Quotex backend.

Why: from a server Quotex cannot be logged into with plain Python (Cloudflare answers 403), and a session copied from
another machine is rejected or expires within hours. Logging in once inside the SERVER's Chrome gives the server its own
session plus long-lived "remember me" cookies, which api_quotex.browser_ws.refresh_session() can use to renew it later.

Run as the service user, under a virtual display (it asks you for the code Quotex emails):

    sudo -u tsignal env QUOTEX_BROWSER_DIR=/opt/tradingsignal/browsers/cft \\
        xvfb-run -a /opt/tradingsignal/venv/bin/python \\
        /opt/tradingsignal/backend/quotex_lib/tools/browser_login.py \\
        --env /opt/tradingsignal/backend/quotex/.env \\
        --out /opt/tradingsignal/backend/quotex/qx_data/sessions/session.json

QX_EMAIL / QX_PASSWORD are read from the --env file (never from the command line). Nothing secret is printed.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

LOGGED_IN = re.compile(r"/(trade|demo-trade|cabinet)")
FORM = '#tab-1 form[action$="/sign-in/"]'
SKIP_FIELDS = {"email", "password", "_token", "remember"}
CHALLENGE_MARKERS = ("just a moment", "attention required", "access denied", "verify you are human")


def say(text: str) -> None:
    print(text, flush=True)


def visible_code_input(driver):
    """A visible text-like input that is not email/password: the box for the emailed code."""
    from selenium.webdriver.common.by import By
    for el in driver.find_elements(By.CSS_SELECTOR, "input"):
        try:
            kind = (el.get_attribute("type") or "text").lower()
            name = (el.get_attribute("name") or "").lower()
            if kind in ("text", "tel", "number") and name not in SKIP_FIELDS and el.is_displayed() and el.is_enabled():
                return el
        except Exception:
            continue
    return None


def page_hint(driver) -> str:
    try:
        text = driver.execute_script("return (document.body && document.body.innerText || '').replace(/\\s+/g,' ').slice(0,260)")
    except Exception:
        text = ""
    return f"url={driver.current_url!r} title={driver.title!r} text={text!r}"


def type_into(driver, el, text: str) -> None:
    """Type text into an input and make sure it arrived (send_keys is sometimes silently ignored under xvfb/headless)."""
    try:
        el.clear()
        el.send_keys(text)
    except Exception:
        pass
    if (el.get_attribute("value") or "") != text:
        # set it the way the page's own code would see a user edit
        driver.execute_script(
            "const el = arguments[0], v = arguments[1];"
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);"
            "el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));",
            el, text)


def submit_code_form(driver, box) -> None:
    """Submit the form holding the code box. Clicking is tried first; if the page did not react (clicks and Enter were
    ignored on this page in some headless setups), submit the form directly."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    try:
        box.find_element(By.XPATH, "./ancestor::form//*[self::button or (self::input and @type='submit')]").click()
    except Exception:
        pass
    time.sleep(3)
    try:
        still_there = box.is_displayed() and LOGGED_IN.search(driver.current_url or "") is None
    except Exception:
        still_there = False            # the old page is gone: the click worked
    if still_there:
        try:
            driver.execute_script("const f = arguments[0].form; if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); }", box)
        except Exception:
            box.send_keys(Keys.ENTER)


def wait_for_cloudflare(driver, seconds: float = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(m in (driver.page_source or "").lower() for m in CHALLENGE_MARKERS):
            return True
        time.sleep(1)
    return False


def login(args) -> Optional[Dict[str, Any]]:
    from dotenv import dotenv_values
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    from api_quotex.browser_ws import _new_driver, build_ssid
    from api_quotex.login import _extract_token_from_trade

    env = dotenv_values(args.env) if args.env else {}
    email = os.environ.get("QX_EMAIL") or env.get("QX_EMAIL")
    password = os.environ.get("QX_PASSWORD") or env.get("QX_PASSWORD")
    if not email or not password:
        say("QX_EMAIL / QX_PASSWORD not found (use --env /path/to/.env).")
        return None

    base = args.base_url.rstrip("/")
    sign_in = f"{base}/{args.lang}/sign-in/"
    target = f"{base}/{args.lang}/{'demo-trade' if args.demo else 'trade'}"

    say("Starting Chrome ...")
    driver, profile = _new_driver()
    try:
        driver.set_script_timeout(30)
        say(f"Opening {sign_in}")
        driver.get(sign_in)
        time.sleep(4)
        if not wait_for_cloudflare(driver):
            say("Cloudflare did not let this Chrome through: " + page_hint(driver))
            return None
        try:
            WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.CSS_SELECTOR, FORM)))
        except Exception:
            say("The sign-in form did not appear: " + page_hint(driver))
            return None

        form = driver.find_element(By.CSS_SELECTOR, FORM)
        type_into(driver, form.find_element(By.CSS_SELECTOR, 'input[name="email"]'), email)
        type_into(driver, form.find_element(By.CSS_SELECTOR, 'input[name="password"]'), password)
        say("Credentials entered, signing in ...")
        form.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()

        deadline = time.time() + args.timeout
        asked = 0
        while time.time() < deadline:
            time.sleep(2)
            if LOGGED_IN.search(driver.current_url or ""):
                break
            box = visible_code_input(driver)
            if box is not None and asked < 3:
                say("Quotex is asking for a code (it was emailed to you). " + page_hint(driver))
                code = input("Enter the code: ").strip()
                asked += 1
                type_into(driver, box, code)
                submit_code_form(driver, box)
                time.sleep(4)
                say("After sending the code: " + page_hint(driver))
        else:
            say("Still not logged in after waiting. Last page: " + page_hint(driver))
            return None

        say("Logged in, reading the session ...")
        driver.get(target)
        time.sleep(6)
        token = _extract_token_from_trade(driver.page_source)
        if not token:
            try:
                token = driver.execute_script("return (window.settings && window.settings.token) || null")
            except Exception:
                token = None
        cookies = driver.get_cookies()
        jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value"))
        session_cookie = next((c["value"] for c in cookies if c["name"].lower() in ("session", "ssid", "qx_session")), None)
        source = session_cookie or token
        if not source:
            say("Logged in but no session token was found: " + page_hint(driver))
            return None
        data = {"token": token, "cookies": jar, "is_demo": bool(args.demo),
                "user_agent": driver.execute_script("return navigator.userAgent"), "ssid": build_ssid(source, args.demo)}
        say(f"Session captured (token {'yes' if token else 'no'}, {len(cookies)} cookies: "
            + ", ".join(sorted({c['name'] for c in cookies if not c['name'].startswith(('_ga', '__cf', 'cf_'))})) + ")")
        return data
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="path of the .env file holding QX_EMAIL and QX_PASSWORD")
    ap.add_argument("--out", required=True, help="where to write session.json")
    ap.add_argument("--live", dest="demo", action="store_false", help="use the real-money account (default: demo)")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--base-url", default="https://quotex.com")
    ap.add_argument("--timeout", type=float, default=300, help="seconds to wait for the login (including typing the code)")
    ap.set_defaults(demo=True)
    args = ap.parse_args()

    data = login(args)
    if not data:
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    try:
        os.chmod(args.out, 0o600)
    except OSError:
        pass
    say(f"Saved {args.out}. Now start the Quotex service.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
