# TradingSignal project notes

Saved at the end of the working session (24 Oct 2026). Everything is stopped; nothing is lost, all files are on disk.

## Folders
| Folder | What it is |
|---|---|
| `frontend/landing/` | tradingsignal landing page and the shared login (`login/`) |
| `frontend/pocketoption/` | Pocket Option terminal (static site, same design as Quotex) |
| `frontend/quotex/` | Quotex terminal (static site) |
| `backend/pocketoption/` | Pocket Option backend (FastAPI, port 8000), `relay/` helper scripts |
| `backend/quotex/` | Quotex backend (FastAPI, port 8002) built on `quotex_lib/` |
| `backend/quotex_lib/` | Quotex client library, collector, backtest, and the shared virtualenv `venv/` that runs everything |
| `backend/gateway/` | `dev_gateway.py`, local stand-in for Nginx (port 8080) |
| `backend/deploy/` | Nginx file and two systemd services for the VPS (isolated, safe next to other sites) |
| `backend/docs/` | older guides (INSTALLATION, HOSTINGER_VPS, TECHNICAL ...) written before this layout; folder names in them are outdated |
| `backups/` | zip backup, the old Pocket Option frontend, old run scripts, and the Pocket Option git history (`AUROX-git-history`, use `git --git-dir=backups/AUROX-git-history log`) |
| `start_all.bat` | starts all services (`start_all.bat noquotex` skips the Quotex backend) |

## Run locally
1. Double click `start_all.bat` (or run it in a terminal).
2. Open http://localhost:8080 .
3. Landing page -> Access Terminal -> pick Quotex or Pocket Option -> sign in.

Ports: Pocket Option backend 8000, Quotex backend 8002, site gateway 8080.

## How it fits together
- Login page verifies the ID with the chosen broker's backend and gets a signed session token (HMAC, 12 h).
  `/signal` and `/correlations` need that token. `SESSION_SECRET` should be set in each backend `.env` for stable sessions.
- Signal engines: the **Quotex** backend uses `v3-reversion-plus` (RSI, Bollinger %B and Stochastic RSI mean-reversion, see `implement.md`);
  the **Pocket Option** backend keeps its original RSI / EMA 9/21 / MACD vote. In both, the top three pairs go to Claude (`backend/*/claude_judge.py`) which picks one or rejects all.
  Needs `ANTHROPIC_API_KEY` in the backend `.env`. Without it the plain indicator ranking is used.
  `CLAUDE_JUDGE_MODEL` defaults to `claude-opus-5-5`; `claude-haiku-5-5` is faster and cheaper.
- Quotex data is live Quotex OTC data (Quotex's own prices, not real market prices). Quotex candle history lags about 5 minutes,
  so recent candles are built from the live tick stream; allow about 6 minutes of warm up after a start.
- Quotex rate limits the account (HTTP 429) if it is reconnected too often. The feed backs off from 30 s up to 10 min.
  Do not restart the Quotex backend repeatedly.

## Still to do before real deployment
- Rotate the Telegram session that was in git history of `backend/pocketoption/.env.example` (the file itself is cleaned).
- Set `SESSION_SECRET`, `ADMIN_KEY`, `ACCESS_IDS` (Quotex logins), `ANTHROPIC_API_KEY` in the backend `.env` files on the server.
- Pocket Option needs a real `LO_UID` token in `backend/pocketoption/.env`; until then it shows the Binance crypto fallback.
- Deploy on the VPS by following `backend/docs/DEPLOY_VPS.md` (subdomain + isolated user + own Nginx file; does not touch the other sites).
- Replace the plain broker sign up links in `frontend/landing/login/login.js` with your referral links if you have them.
- Move the project out of the OneDrive folder if edits keep reverting (it happened once during the session).
