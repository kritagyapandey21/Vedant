"""Configuration for the AUROX Quotex signal system."""

import os
from pathlib import Path

# Indicator confidence weights (used by analysis.py)
CONFIDENCE_WEIGHTS = {
    "rsi_signal":        0.375,
    "ema_trend":         0.375,
    "macd_confirmation": 0.25,
}
MIN_CONFIDENCE_THRESHOLD = 40

# Server
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:8000,http://127.0.0.1:3000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

# Access control: comma separated list of IDs allowed to log in.
# Empty = any non-empty ID is accepted (local/dev use).
ACCESS_IDS = {i.strip().casefold() for i in os.environ.get("ACCESS_IDS", "").split(",") if i.strip()}

# Quotex data feed
QX_EMAIL = os.environ.get("QX_EMAIL", "")
QX_PASSWORD = os.environ.get("QX_PASSWORD", "")
QX_IS_DEMO = os.environ.get("QX_IS_DEMO", "true").lower() != "false"   # data only - the app never trades
QX_MIN_PAYOUT = int(os.environ.get("QX_MIN_PAYOUT", "80"))
QX_ASSET_TYPES = {t.strip() for t in os.environ.get("QX_ASSET_TYPES", "currency").split(",") if t.strip()}
QX_REAL_MARKET_ONLY = os.environ.get("QX_REAL_MARKET_ONLY", "false").lower() == "true"   # drop *_otc pairs (Quotex's own prices)
QX_REFRESH_SECONDS = int(os.environ.get("QX_REFRESH_SECONDS", "60"))
QX_CANDLES = 150                                  # 1-minute candles per pair
CACHE_STALE_SECONDS = int(os.environ.get("CACHE_STALE_SECONDS", "180"))

# Where api_quotex keeps sessions/ and its log (it uses paths relative to the CWD)
QX_DATA_DIR = Path(os.environ.get("QX_DATA_DIR", Path(__file__).with_name("qx_data")))
# Location of the backend/quotex_lib checkout, used when api_quotex is not pip-installed
QX_LIB_PATH = os.environ.get("QX_LIB_PATH", str(Path(__file__).resolve().parents[1] / "quotex_lib"))
