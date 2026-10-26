"""
AUROX AI - Quotex edition.
FastAPI backend that turns Quotex 1-minute candles into a single best BUY/SELL signal for the next candle.
Signal generation only: this service never places orders.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from analysis import IST, SignalAnalyzer
from config import (
    ACCESS_IDS, ADMIN_KEY, ALLOWED_ORIGINS, BYPASS_TRADER_ID, CACHE_STALE_SECONDS, HOST, LOG_FORMAT,
    LOG_LEVEL, MIN_CONFIDENCE_THRESHOLD, PORT, QX_IS_DEMO,
)
import claude_judge
import session_auth
import signal_v3
import telegram_service
from quotex_feed import QuotexFeed
from timezone_utils import is_valid_timezone, timezone_catalog
from trader_store import get_timezone, set_timezone

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "quotex"
MIN_LEAD_SECONDS = 3          # entry must be at least this far in the future

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AUROX AI Quotex", description="1-minute signals from Quotex candles", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

feed = QuotexFeed()
require_session = session_auth.make_dependency("quotex")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cache-Control"] = "no-store"
    return response


async def verify_admin_key(x_admin_key: Optional[str] = Header(None)):
    if ADMIN_KEY and x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


class SignalResponse(BaseModel):
    pair: str
    direction: str
    entry_time: str
    expiry_time: str
    confidence: float
    current_price: float
    indicator_scores: Dict[str, float]
    generated_at: str
    candle_duration: str
    data_source: str
    engine: str = ""
    chart_candles: List[Dict[str, float]] = []
    ai_review: Optional[str] = None
    entry_timestamp: str
    expiry_timestamp: str
    generated_timestamp: str


class ServerTimeResponse(BaseModel):
    server_time: str
    unix_timestamp: int
    next_candle_entry: str
    next_candle_expiry: str
    server_timestamp: str
    next_candle_entry_timestamp: str
    next_candle_expiry_timestamp: str


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    pairs_monitored: int
    data_source: str
    session_active: bool


def next_candle_times(now: datetime):
    """Entry = next full minute (pushed one more minute if it is too close), expiry = entry + 1 min."""
    entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if (entry - now).total_seconds() < MIN_LEAD_SECONDS:
        entry += timedelta(minutes=1)
    return entry, entry + timedelta(minutes=1)


def cache_age() -> float:
    if feed.updated_at is None:
        return -1
    return (datetime.now(timezone.utc) - feed.updated_at).total_seconds()


def cache_fresh() -> bool:
    age = cache_age()
    return feed.connected and 0 <= age < CACHE_STALE_SECONDS


@app.on_event("startup")
async def startup_event():
    logger.info("AUROX AI (Quotex) starting - %s account, data only", "demo" if QX_IS_DEMO else "LIVE")
    asyncio.create_task(feed.run())


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        pairs_monitored=len(feed.candles),
        data_source="Quotex",
        session_active=feed.connected,
    )


@app.get("/server-time", response_model=ServerTimeResponse)
async def get_server_time() -> ServerTimeResponse:
    now = datetime.now(IST)
    entry, expiry = next_candle_times(now)
    return ServerTimeResponse(
        server_time=now.strftime("%H:%M:%S"),
        unix_timestamp=int(now.timestamp()),
        next_candle_entry=entry.strftime("%H:%M:%S"),
        next_candle_expiry=expiry.strftime("%H:%M:%S"),
        server_timestamp=now.isoformat(),
        next_candle_entry_timestamp=entry.isoformat(),
        next_candle_expiry_timestamp=expiry.isoformat(),
    )


@app.get("/signal", response_model=SignalResponse)
@limiter.limit("30/minute")
async def get_signal(request: Request, _session: Optional[dict] = Depends(require_session)) -> SignalResponse:
    if not cache_fresh():
        detail = "Quotex data feed is not ready yet" if feed.updated_at is None else "Quotex data is stale, reconnecting"
        raise HTTPException(status_code=503, detail=detail)

    analyses = []
    for symbol in list(feed.candles):
        closes = feed.closed_closes(symbol)
        # need 60 gap-free candles and a recent last close, otherwise the data is still warming up / stale
        if len(closes) < 60 or feed.last_closed_age(symbol) > 150:
            continue
        analysis = SignalAnalyzer.analyze_pair({
            "symbol": feed.names.get(symbol, symbol),
            "closes": closes,
            "current_price": feed.price(symbol) or closes[-1],
        })
        if analysis:
            analysis["_symbol"] = symbol
            analysis["closes"] = closes[-30:]
            analyses.append(analysis)

    if not analyses:
        raise HTTPException(
            status_code=503,
            detail="Live candles are still warming up (about 6 minutes after start) or no pair has a clear setup",
        )
    best = SignalAnalyzer.select_best_signal(analyses)
    if not best:
        raise HTTPException(status_code=503, detail="No viable signals generated")

    # Claude reviews the top three and may pick a different one, or reject them all
    shortlist = sorted((x for x in analyses if x["confidence"] >= MIN_CONFIDENCE_THRESHOLD),
                       key=lambda x: x["confidence"], reverse=True)[:3]
    verdict = await claude_judge.review(shortlist)
    ai_note = None
    if verdict is not None:
        if verdict["pick"] is None:
            raise HTTPException(status_code=503, detail="AI review found no setup worth taking right now")
        best, ai_note = shortlist[verdict["pick"]], verdict["reason"]

    entry, expiry = next_candle_times(datetime.now(IST))
    signal = SignalAnalyzer.generate_signal(best, entry, expiry)
    signal["data_source"] = "Quotex"
    signal["ai_review"] = ai_note
    signal["chart_candles"] = feed.chart(best["_symbol"])
    logger.info("Signal: %s %s confidence=%.1f%% from %d pairs",
                signal["pair"], signal["direction"], signal["confidence"], len(analyses))
    return SignalResponse(**signal)


@app.get("/pairs")
async def get_pairs() -> Dict:
    return {"data_source": "quotex", "assets": list(feed.candles), "total": len(feed.candles)}


@app.get("/monitor")
async def get_monitor(_session: Optional[dict] = Depends(require_session)) -> Dict:
    """Live per-pair oscillator readings, the same numbers the signal engine works from."""
    pairs = []
    for symbol in list(feed.candles):
        closes = feed.closed_closes(symbol)
        snap = signal_v3.snapshot(closes)
        pairs.append({
            "symbol": symbol,
            "name": feed.names.get(symbol, symbol),
            "price": feed.price(symbol) or (closes[-1] if closes else None),
            "candles": len(closes),
            "ready": snap is not None,
            **(snap or {}),
        })
    pairs.sort(key=lambda p: abs(p.get("score", 0)), reverse=True)
    for p in pairs[:8]:                       # chart data only for the pairs the panel will draw
        if p["ready"]:
            p["series"] = signal_v3.series(feed.closed_closes(p["symbol"]))
    return {"engine": signal_v3.ENGINE, "min_candles": signal_v3.MIN_CANDLES, "pairs": pairs, "fresh": cache_fresh()}


@app.get("/status")
async def get_status() -> Dict:
    return {
        "status": "operational",
        "data_source": "quotex",
        "cache_valid": cache_fresh(),
        "cache_age_seconds": int(cache_age()),
        "cached_items": len(feed.candles),
        "connected": feed.connected,
        "last_error": feed.last_error,
    }


@app.get("/correlations")
async def get_correlations(_session: Optional[dict] = Depends(require_session)) -> Dict:
    """Pearson correlation between per-minute returns of the active pairs."""
    series = {}
    for symbol in feed.candles:
        closes = feed.closed_closes(symbol)
        returns = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
        if len(returns) >= 10:
            series[symbol] = returns

    nodes = [{"id": s, "label": s.replace("_otc", "")} for s in series]
    links = []
    symbols = list(series)
    for i, first in enumerate(symbols):
        for second in symbols[i + 1:]:
            size = min(len(series[first]), len(series[second]))
            x, y = series[first][-size:], series[second][-size:]
            mx, my = sum(x) / size, sum(y) / size
            num = sum((a - mx) * (b - my) for a, b in zip(x, y))
            den = (sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)) ** 0.5
            corr = num / den if den else 0
            if abs(corr) >= 0.05:
                links.append({"source": first, "target": second, "value": round(corr, 3)})
    return {"data_source": "quotex", "nodes": nodes, "links": links}


@app.post("/verify-trader")
@limiter.limit("10/minute")
async def verify_trader(request: Request, payload: Dict) -> Dict:
    """Check the ID (ACCESS_IDS list if set, then the Quotex partner bot if REQUIRE_VERIFICATION=true) and remember its timezone."""
    trader_id = str(payload.get("trader_id", "")).strip()
    if not trader_id:
        raise HTTPException(status_code=400, detail="trader_id required")
    if len(trader_id) > 64 or not trader_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="trader_id contains invalid characters")
    timezone_name = payload.get("timezone")
    if timezone_name is not None and not is_valid_timezone(timezone_name):
        raise HTTPException(status_code=400, detail="Invalid IANA timezone")

    if ACCESS_IDS and trader_id.casefold() not in ACCESS_IDS:
        return {"found": False, "message": "ACCESS DENIED, ID NOT AUTHORISED"}
    # the Quotex partner bot decides (open when REQUIRE_VERIFICATION is not true)
    if BYPASS_TRADER_ID and trader_id.casefold() == BYPASS_TRADER_ID.casefold():
        result = {"found": True, "bypassed": True, "message": "TRADER VERIFIED"}
    else:
        result = await telegram_service.verify_trader(trader_id)
    if result.get("found"):
        result["timezone"] = set_timezone(trader_id, timezone_name or get_timezone(trader_id))
        result["token"] = session_auth.issue(trader_id, "quotex")
    return result


@app.get("/timezones")
async def get_timezones() -> List[Dict]:
    return timezone_catalog()


@app.post("/refresh-session")
@limiter.limit("3/hour")
async def refresh_session(request: Request, _: None = Depends(verify_admin_key)) -> Dict:
    """Drop the Quotex connection so the feed re-logs in on its next loop."""
    feed.request_reconnect()
    return {"status": "ok", "message": "Feed will reconnect on its next cycle"}


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Serve the UI from the same process (local use). Mounted last so API routes win.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
