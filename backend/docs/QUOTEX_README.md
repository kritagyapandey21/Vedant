# Pulse QX (AUROX Quotex edition)

Pulse QX: the signal engine of AUROX with a new interface (light theme, sidebar desk, signal ticket), with Quotex as the data source (via the sibling `API-Quotex` library).
It **only reads market data** - it never places orders. Signals are not financial advice; the sibling
`API-Quotex/backtest.py` found no statistically proven edge for RSI/EMA/MACD-style rules on 1-minute data.

## Run (Windows)
    run_backend.bat          # then open http://localhost:8000

The backend uses the `API-Quotex` virtualenv (needs `fastapi uvicorn slowapi python-dotenv tzdata`, already installed).
Config lives in `backend/.env` (see `.env.example`). `QX_DATA_DIR` points at the folder holding `sessions/`
(Quotex credentials + saved session); by default that is the API-Quotex checkout.

## How it works
- `backend/quotex_feed.py` logs in (Playwright, via api_quotex), keeps a connection, every minute pulls 150 candles for
  every open pair with payout >= `QX_MIN_PAYOUT` (default 80) of type `QX_ASSET_TYPES` (default `currency`).
- Quotex candle history lags ~5 minutes, so the newest candles are built from the live tick stream and merged over the
  history. Only the latest gap-free run of *closed* candles is analysed. After a (re)start allow ~6 minutes of warm-up.
- A rejected session is detected by a failed connect and refreshed with a forced re-login (api_quotex's own validation
  accepts expired sessions). Re-login is throttled to once per 10 minutes.
- `backend/analysis.py` / `indicators.py` (unchanged from AUROX): RSI + EMA9/21 + MACD vote, weighted confidence,
  best pair above 40%. Entry = next full minute, expiry = +1 minute (IST; the UI converts to the viewer's timezone).

## Differences from the Pocket Option version
- No Telegram/PocketPartners check. Set `ACCESS_IDS=id1,id2` to restrict logins; empty = any ID.
- Single process: the backend also serves the frontend at `/`.
- Pair list is Quotex (mostly OTC) pairs; no relay scripts needed.
