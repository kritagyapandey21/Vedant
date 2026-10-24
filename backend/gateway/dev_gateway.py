"""Local stand-in for the production Nginx: one port for the whole site.

  /               -> frontend/landing
  /pocketoption/  -> frontend/pocketoption  (API at /api -> backend/pocketoption on :8000)
  /quotex/        -> frontend/quotex        (API at /quotex-api -> backend/quotex on :8002)

    python dev_gateway.py [port]
"""
import mimetypes
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
STATIC = {"/pocketoption/": FRONTEND / "pocketoption", "/quotex/": FRONTEND / "quotex", "/": FRONTEND / "landing"}
PROXY = {"/quotex-api/": "http://127.0.0.1:8002/", "/api/": "http://127.0.0.1:8000/"}
HOP = {"transfer-encoding", "connection", "content-length", "server", "date"}

# Baseline headers for every page; strict CSP only for the landing page + login (the terminals use inline scripts).
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'self'; form-action 'self'; object-src 'none'")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _proxy(self, prefix, target):
        url = target + self.path[len(prefix):]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or None
        req = urllib.request.Request(url, data=body, method=self.command)
        for k in ("Content-Type", "X-Admin-Key", "Authorization"):
            if self.headers.get(k):
                req.add_header(k, self.headers[k])
        req.add_header("X-Forwarded-For", self.client_address[0])
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception:
            self.send_error(502, "backend not reachable")
            return
        data = resp.read()
        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
        for k, v in resp.headers.items():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self):
        path = self.path.split("?")[0]
        for prefix, folder in STATIC.items():
            if path.startswith(prefix):
                rel = path[len(prefix):] or "index.html"
                if any(part.startswith(".") for part in rel.split("/")):
                    break
                f = (folder / rel).resolve()
                if f.is_dir():
                    f = f / "index.html"
                if folder.resolve() in f.parents and f.is_file():
                    data = f.read_bytes()
                    self.send_response(200)
                    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                    if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                        ctype += "; charset=utf-8"
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    for k, v in BASE_HEADERS.items():
                        self.send_header(k, v)
                    if folder == STATIC["/"]:
                        self.send_header("Content-Security-Policy", CSP)
                    self.end_headers()
                    self.wfile.write(data)
                    return
                break
        self.send_error(404)

    def _handle(self):
        if self.path in ("/pocketoption", "/quotex"):
            self.send_response(301)
            self.send_header("Location", self.path + "/")
            self.end_headers()
            return
        for prefix, target in PROXY.items():
            if self.path.startswith(prefix):
                return self._proxy(prefix, target)
        if self.command in ("GET", "HEAD"):
            return self._static()
        self.send_error(405)

    do_GET = do_POST = do_HEAD = _handle


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Site gateway on http://localhost:{port}  (landing, /pocketoption/, /quotex/)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
