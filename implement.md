# How the signal is calculated (implementation guide)

This file describes how TANIX 3.0 produces a trade signal: where the data comes from, the exact maths, how the
best pair is chosen, what the API returns, and what is needed to run it. It reflects the code as of 2026-10-24.
No secrets are stored in this file.

**One engine only: `v3-reversion-plus`** ([backend/signal_v3.py](backend/signal_v3.py), wrapped by
[backend/analysis.py](backend/analysis.py)). It is a rule-based mean-reversion strategy. There is no machine learning,
no training and no stored model: every signal is recomputed from the latest candles on each request.

> **Honest status:** measured win rate on real Pocket Option candles is about 50-51% (see section 10). Break-even is
> roughly 52-56%. The engine has no proven edge. Do not describe it to traders as "accurate".

---

## 1. Big picture

```
Pocket Option  --(WebSocket, token LO_UID)-->  _po_data_loop  -->  relay_candle_cache (in memory)
                                                                          |
trader clicks "Get signal" --> GET /signal --> for each forex pair: SignalAnalyzer.analyze_pair
                                                      |  (signal_v3: RSI + Bollinger + StochRSI)
                                                      v
                                       keep pairs where all 3 agree -> pick the most stretched one
                                                      v
                                   add entry/expiry times (next full minute, +1 min) -> JSON -> frontend
```

Idea in one line: when a price has been pushed unusually far in one direction over the last minutes, bet on a
small snap-back over the next one-minute candle. Stretched down gives BUY (CALL), stretched up gives SELL (PUT).

## 2. Data source

| Item | Value | Where |
|---|---|---|
| Provider | Pocket Option, via the `binaryoptionstoolsv2==0.2.11` library | [app.py:201](backend/app.py#L201) |
| Credential | A logged-in session token in `LO_UID` (or `PO_SESSION_TOKEN`) in `backend/.env` | [app.py:346](backend/app.py#L346) |
| Candle size | 1 minute (`PERIOD = 60`) | app.py |
| History pulled | 9000 seconds = 150 candles per pair (`DURATION`) | app.py |
| History kept | Latest 200 candles per pair, in memory only | app.py:274 |
| Refresh | Every 60 seconds, 0.5 s pause between pairs | app.py |
| Asset filter | Payout of at least 80% **and** fiat/fiat forex only (EUR, USD, GBP, JPY, AUD, CAD, CHF, NZD). Crypto and metals are dropped | `_is_forex_pair`, app.py:174 |
| Staleness | Cache older than 300 s (`RELAY_STALE_SECONDS`) is not used for signals | app.py:169 |
| Self-healing | Two empty refresh cycles in a row force a reconnect; connection errors retry after 30 s | app.py:295-332 |

**Important data limitation:** Pocket Option gives one price per minute. Open, high, low and close are the same
number, so candles are close-to-close and have no wicks. Every indicator below therefore uses **closing prices only**.
Volume is always stored as 0.

**Fallback (dev only):** with no token the backend runs in crypto mode using Gate.io 1-minute candles
([backend/market_data.py](backend/market_data.py), 100 candles for BTC, ETH, BNB, ADA, XRP, DOGE, LINK, LTC, BCH, SOL).
The same engine is applied to them. There is also a relay path (`POST /candle-data`, [relay/](relay/)) that lets a
separate machine push candles in; the live server does not need it.

## 3. What the engine needs per pair

- At least **60 closing prices** (`len(c) < 60` returns no signal) and all values finite.
- Nothing else: no volume, no highs/lows, no news, no other pairs.
- A pair with fewer than 60 candles is skipped silently ([app.py:567](backend/app.py#L567)).

## 4. Indicators (exact formulas)

All are plain numpy functions in [signal_v3.py](backend/signal_v3.py). `c` is the array of closes, `c[-1]` is the latest.

### 4.1 The three that decide the call (oscillators)

Each is turned into a score in **[-1, +1]** where **positive = oversold = expect a bounce up**, negative = overbought.

| Indicator | Raw value | Score formula | Notes |
|---|---|---|---|
| **RSI (14)** | Wilder RSI: seed with the simple average of the first 14 gains and losses, then `avg = (avg*13 + new)/14`. `RSI = 100 - 100/(1 + avgGain/avgLoss)`; 100 if no losses | `rsi_mr = clip((50 - RSI) / 25)` | RSI 25 gives +1, RSI 50 gives 0, RSI 75 gives -1 |
| **Bollinger %B (20, 2σ)** | Mean and standard deviation of the last 20 closes. `%B = (price - (mean - 2σ)) / (4σ)`; 0.5 if σ is 0 | `bb_mr = clip((0.5 - %B) * 2)` | %B 0 (on the lower band) gives +1; %B 1 (upper band) gives -1 |
| **Stochastic RSI (14)** | Where today's RSI sits inside the min-max range of the last 14 RSI values: `(RSI - min) / (max - min)`; 0.5 if the range is flat or fewer than 3 values | `sr_mr = clip((0.5 - StochRSI) * 2)` | StochRSI 0 gives +1; 1 gives -1 |

`clip(x)` limits a value to [-1, +1].

### 4.2 Context only (computed, shown in the panel, NOT used to decide)

| Indicator | Definition | Output |
|---|---|---|
| **Supertrend** (period 10, multiplier 2.0) | Close-only version: ATR is the Wilder average of `abs(close - previous close)`; bands are `close ± 2·ATR`, ratcheted like the standard indicator; trend flips when the close crosses the opposite band | Direction (+1/-1), distance from the line in ATRs, bars since last flip. Score `= dir * clip(0.6 + 0.2*min(dist,2) - 0.02*max(age-5,0), 0.2, 1.0)` |
| **EMA stack 9/21/50** | `scale = price * 0.0004` (about 4 basis points) | `0.7*clip((EMA9-EMA21)/scale) + 0.3*clip((price-EMA50)/(2*scale))` |
| **MACD (12, 26, 9) histogram** | `h_scale` = largest absolute histogram value in the last 30 bars | `0.6*clip(h/h_scale) + 0.4*clip((h - h_prev)/h_scale*2)` |
| **Kaufman Efficiency Ratio (10)** | `abs(close[-1] - close[-11]) / sum of abs(1-bar changes over those 10 bars)`. 1 = straight line, 0 = pure chop | Shown as "strength"; regime label: RANGING if at most 0.33, TRENDING if at least 0.5, else MIXED |
| **Shock** | Latest 1-minute move divided by the average 1-minute move over the previous 30 bars | Only used if the optional `min_shock` filter is on (it is off) |

## 5. The decision rule (`reversion_plus`)

```
score = 0.40 * rsi_mr + 0.35 * bb_mr + 0.25 * sr_mr          # range -1 .. +1
direction d = +1 (CALL/BUY) if score > 0 else -1 (PUT/SELL)

signal only if ALL of these hold:
  1. abs(score) >= 0.25                                          # min_score
  2. rsi_mr, bb_mr, sr_mr all have the same sign as d and none is exactly 0   # need_agree
otherwise: no signal for this pair
```

Parameters used live ([signal_v3.py:197](backend/signal_v3.py#L197)): `min_score=0.25`, `need_agree=True`.
Two further filters exist but are **switched off** (`None`): `ker_max` (skip clean trends) and `min_shock` (only fade
a big last-minute jump). Backtests showed both lowered accuracy.

### Worked example (illustrative numbers)

RSI = 22, %B = 0.05, StochRSI = 0.10:

- `rsi_mr = clip((50-22)/25) = clip(1.12) = 1.00`
- `bb_mr = (0.5-0.05)*2 = 0.90`
- `sr_mr = (0.5-0.10)*2 = 0.80`
- `score = 0.4*1.00 + 0.35*0.90 + 0.25*0.80 = 0.915`, which is above 0.25 and all three are positive, so **CALL / BUY**.
- Display readings: `osc_rsi = 100`, `osc_bollinger = 95`, `osc_stoch_rsi = 90`, so "setup strength" = (100+95+90)/3 = **95.0**.

## 6. Setup strength (the number shown as "confidence")

Computed in [analysis.py:28-55](backend/analysis.py#L28).

```
agree(x) = clamp( 50 + 50 * x * d , 0, 100 )        # x = one oscillator score, d = +1 or -1
osc_rsi, osc_bollinger, osc_stoch_rsi = agree(rsi_mr), agree(bb_mr), agree(sr_mr)
confidence = min( 99 , (osc_rsi + osc_bollinger + osc_stoch_rsi) / 3 )
```

- It is the average of the three readings that decided the call, so it can be checked against the bars on screen.
- It measures **how stretched the price is, not the probability of winning.** Measured win rate is about 51% no matter
  how high this number is.
- Because all three oscillators must agree, every real signal scores above 50. The `MIN_CONFIDENCE_THRESHOLD = 40`
  in [config.py](backend/config.py) therefore never filters anything.
- The terminal panel labels it "SETUP STRENGTH"; the "New signal ready" toast still says "confidence".

## 7. Choosing the pair and timing

1. For every cached forex pair with at least 60 candles, run `analyze_pair`. Pairs with no signal are dropped.
2. `select_best_signal` keeps those with confidence of at least 40 (always true) and returns the one with the
   **highest setup strength**, meaning the most over-stretched pair right now.
3. If no pair qualifies the API answers `503 No viable signals generated` (or `Could not analyze any PO assets`).
4. Timing ([pocket_option_api.py:277](backend/pocket_option_api.py#L277), same logic in `market_data.py`):
   entry = the next full minute (current minute + 1 if any seconds have passed), expiry = entry + 1 minute.
   Times are India time (IST). Candle duration is fixed at 1 minute.
5. `current_price` shown is the live price (`relay_latest_prices`); the analysis itself uses the candle closes.
6. The signal also carries the last 60 closes as `chart_candles`, so the terminal can draw the market chart.
   Real-market pairs with no OTC name (e.g. `AUDCHF`) fall back to their own symbol for the chart.

## 8. API contract

`GET /signal` (needs a valid trader session token, limited to 30 requests per minute per client) returns:

```json
{
  "pair": "EUR/USD-OTC",
  "direction": "BUY",                  // CALL -> BUY, PUT -> SELL
  "entry_time": "14:31:00", "expiry_time": "14:32:00",
  "entry_timestamp": "...", "expiry_timestamp": "...", "generated_timestamp": "...",
  "confidence": 95.0,                  // = setup strength, section 6
  "current_price": 1.08123,
  "indicator_scores": {
    "osc_rsi": 100.0, "osc_bollinger": 95.0, "osc_stoch_rsi": 90.0,   // decide the call
    "rsi": 98.0,                       // the three combined: agree(score)
    "ema": 41.0, "macd": 55.0, "supertrend": 30.0,                    // context, 0-100, above 50 supports the call
    "strength": 24.0                   // Efficiency Ratio x 100 (trend steadiness), not a vote
  },
  "generated_at": "14:30:12", "candle_duration": "1 minute",
  "data_source": "Pocket Option", "engine": "v3-reversion-plus",
  "chart_candles": [{"time": 1760000000, "close": 1.0812}, "... 60 items"]
}
```

(Values above are examples.) Related endpoints: `/server-time` (clock sync and next entry/expiry), `/pairs`,
`/correlations` (Pearson correlation of pair returns for the neural map), `/status` and `/health`.

## 9. What is needed to run it

- **Python** 3.x with the packages in [backend/requirements.txt](backend/requirements.txt) (FastAPI, uvicorn, numpy,
  aiohttp, telethon, slowapi, pywebpush, `binaryoptionstoolsv2==0.2.11`, playwright for the optional auto-login).
- **`backend/.env`** (template: [backend/.env.example](backend/.env.example)):
  - `LO_UID` - Pocket Option session token. Required for live signals. One backend per token; never reuse a token
    across servers, and expect it to expire (connection loops without "cache updated" in the log means it needs replacing).
  - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `REQUIRE_VERIFICATION=true` - trader login check
    (not part of the maths).
  - `ADMIN_KEY` - long random value protecting admin endpoints.
  - Optional: `BYPASS_TRADER_ID`, `NOTIFY_TIME`, `NOTIFY_TZ`, `VAPID_SUBJECT`.
- **Run:** `run_backend.bat` / `run_backend.sh` (backend), `run_frontend.bat` / `run_frontend.sh` (static site).
  Production deployment (Nginx, systemd services, ports) is in [HOSTINGER_VPS.md](HOSTINGER_VPS.md) and
  [deploy/](deploy/).
- **To change the engine**, edit only [signal_v3.py](backend/signal_v3.py) (`components`, `reversion_plus`,
  `analyze_reversion_plus`) and [analysis.py](backend/analysis.py) (`analyze_pair`). Keep scores in [-1, +1]
  (positive = bullish) because the panel maps them with `agree()`.

## 10. Measured performance and known limits

Results recorded in [fixes.md](fixes.md) section 6 (the backtest scripts were deleted afterwards):

| Test | Result |
|---|---|
| Binance crypto, 3 weeks of 1-min data, best pair each minute, out-of-sample | old engine 40-43%, Supertrend/EMA 44-48%, v3 adaptive ~51-52%, v3 mean-reversion 54-56% |
| Real Pocket Option candles on the VPS overnight (~463 trades per engine, ±4.6%) | current 49.0%, v3 50.1%, v3-reversion 49.7%, **v3-reversion-plus 51.1%** |
| Pooled across local and VPS runs | v3-reversion-plus about 51% (±4%) |

- A 1-minute binary option needs roughly 52-56% to break even depending on payout, so there is no demonstrated edge.
- Crypto results do not carry over to Pocket Option OTC prices; always judge on Pocket Option data.
- Pocket Option OTC prices are generated by the broker, so classic technical patterns may simply not hold.
- Not yet tried: 3- and 5-minute expiries, signalling only on very strong setups, per-pair or time-of-day effects.
- No signal journal exists yet. Building one (store each signal, score it when the candle closes, show the real win
  rate on `/admin`) is the most useful next step before making any accuracy claim.
- Marketing text on the site ("high-accuracy", "Precise algorithm") overstates what is measured and should be softened.

## 11. File map

| File | Role |
|---|---|
| [backend/signal_v3.py](backend/signal_v3.py) | Indicators, scores, the decision rule |
| [backend/analysis.py](backend/analysis.py) | Turns scores into setup strength and the final signal JSON; picks the best pair |
| [backend/app.py](backend/app.py) | Data loop, `/signal` route, cache, sessions, admin |
| [backend/config.py](backend/config.py) | Pairs list (crypto fallback), thresholds, security settings |
| [backend/market_data.py](backend/market_data.py) | Gate.io fallback data and next-candle timing |
| [backend/pocket_option_api.py](backend/pocket_option_api.py), [po_websocket.py](backend/po_websocket.py), [po_auth.py](backend/po_auth.py), [po_browser.py](backend/po_browser.py) | Pocket Option session, WebSocket and auto-login helpers |
| [frontend/app.js](frontend/app.js) | Terminal UI: calls `/signal`, draws the chart, driver bars and the setup-strength ring |
