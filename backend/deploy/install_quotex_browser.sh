#!/usr/bin/env bash
# Install what the Quotex backend needs to open its socket from a real Chrome (see backend/docs/DEPLOY_VPS.md).
# Run once as root:   bash /opt/tradingsignal/backend/deploy/install_quotex_browser.sh
#
# Isolation: nothing system-wide is replaced.
#  - Chrome for Testing + the matching chromedriver go into /opt/tradingsignal/browsers/cft (your system Chrome is untouched)
#  - apt only installs packages that are MISSING, never upgrades or removes anything
#  - no services, no ports, no fixed X display (xvfb-run -a picks a free one)
set -euo pipefail

APP_USER=tsignal
DEST=/opt/tradingsignal/browsers/cft

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# Ubuntu 24.04+ renamed some libraries with a t64 suffix; use whichever exists here
pick() { if apt-cache show "$1t64" >/dev/null 2>&1; then echo "$1t64"; else echo "$1"; fi; }
PKGS="xvfb xauth unzip curl ca-certificates libnss3 libgbm1 $(pick libasound2) $(pick libatk-bridge2.0-0) $(pick libgtk-3-0) libxss1 libxkbcommon0 fonts-liberation"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
    echo "Installing missing packages:$MISSING"
    apt-get update
    apt-get install -y --no-install-recommends $MISSING
else
    echo "All OS packages already present."
fi

if [ ! -x "$DEST/chrome-linux64/chrome" ]; then
    mkdir -p "$DEST"
    JSON="$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json)"
    python3 - "$JSON" "$DEST" <<'EOF'
import io, json, sys, urllib.request, zipfile
data = json.loads(sys.argv[1])["channels"]["Stable"]
print("Chrome for Testing version:", data["version"])
for kind in ("chrome", "chromedriver"):
    url = next(d["url"] for d in data["downloads"][kind] if d["platform"] == "linux64")
    print("Downloading", url)
    with urllib.request.urlopen(url) as r:
        zipfile.ZipFile(io.BytesIO(r.read())).extractall(sys.argv[2])
EOF
    # zip extraction drops the executable bit; Chrome also needs its helpers (crashpad handler etc.)
    chmod -R u+x "$DEST"
fi
chown -R "$APP_USER:$APP_USER" /opt/tradingsignal/browsers
"$DEST/chrome-linux64/chrome" --version || echo "WARNING: chrome failed to start, a system library is probably missing"
echo "Done. Chrome is in $DEST"
