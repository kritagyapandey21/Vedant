"""Self-test for tools/browser_login.py against a mock Quotex sign-in site (form -> emailed code -> trade page).

No Quotex account or internet needed:   python tests/browser_login_selftest.py
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "tools", "browser_login.py")
PORT = 8768
GOOD_CODE = "123456"

SIGN_IN = """<html><body><div id="tab-1"><form action="/en/sign-in/" method="post">
<input type="hidden" name="_token" value="csrf"><input name="email" type="email"><input name="password" type="password">
<button type="submit">Sign in</button></form></div></body></html>"""
CODE_PAGE = """<html><head><title>Confirm it is you</title></head><body><p>We sent a PIN code to your email</p>
<form action="/en/sign-in/code/" method="post"><input name="code" type="text" placeholder="PIN code"><button type="submit">Continue</button></form></body></html>"""
TRADE = """<html><body><script>window.settings = {"token":"TOK123","csrf":"c"};</script>trading</body></html>"""


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/en/sign-in"):
            return self._send(SIGN_IN)
        if self.path.startswith("/en/demo-trade") or self.path.startswith("/en/trade"):
            if "laravel_session=SESS1" in (self.headers.get("Cookie") or ""):
                return self._send(TRADE)
            return self._send("", 302, [("Location", "/en/sign-in/")])
        self._send("not found", 404)

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path.startswith("/en/sign-in/code"):
            if form.get("code", [""])[0] == GOOD_CODE:
                return self._send("", 302, [("Location", "/en/demo-trade"), ("Set-Cookie", "laravel_session=SESS1; Path=/"),
                                            ("Set-Cookie", "remember_web_x=REM1; Path=/")])
            return self._send(CODE_PAGE)
        if self.path.startswith("/en/sign-in"):
            ok = form.get("email", [""])[0] == "me@example.com" and form.get("password", [""])[0] == "secret"
            return self._send(CODE_PAGE if ok else SIGN_IN)
        self._send("not found", 404)


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = os.path.join(tempfile.mkdtemp(), "session.json")
    env = dict(os.environ, QX_EMAIL="me@example.com", QX_PASSWORD="secret", QUOTEX_BROWSER_HEADLESS="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-W", "ignore", TOOL, "--out", out, "--base-url", f"http://127.0.0.1:{PORT}", "--timeout", "90"],
                          input=GOOD_CODE + "\n", capture_output=True, text=True, env=env, timeout=240)
    print(proc.stdout.strip()[-900:])
    if proc.stderr.strip():
        print("stderr:", proc.stderr.strip()[-400:])
    saved = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    checks = {
        "tool exited 0": proc.returncode == 0,
        "asked for the emailed code": "Enter the code" in proc.stdout,
        "token captured from the trade page": saved.get("token") == "TOK123",
        "ssid built from the token": '"session":"TOK123"' in saved.get("ssid", ""),
        "account cookies saved": "laravel_session=SESS1" in saved.get("cookies", "") and "remember_web_x=REM1" in saved.get("cookies", ""),
        "password never printed": "secret" not in proc.stdout,
    }
    for name, passed in checks.items():
        print(("PASS  " if passed else "FAIL  ") + name)
    server.shutdown()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
