"""Continuously collect closed 1-minute candles for every open pair paying more than a threshold.

    python collector.py                  # demo account, payout > 80
    python collector.py --min-payout 85

How it works
  * Pairs are chosen from the live asset list: open, payout > threshold, 1m supported. The list is
    re-evaluated every cycle; once a pair is followed it keeps being collected.
  * Live data: every followed pair streams ~2 price ticks/second; ticks are aggregated into 1-minute
    candles (bucketed on server time). Only fully observed minutes are saved.
  * The server's history (newest ~200 candles) is used to backfill at start-up and after reconnects.

Storage: data/candles.db (SQLite)
  candles(asset, ts, open, high, low, close, volume)  ts = minute open time, UTC epoch seconds;
                                                      volume = tick count for that minute
  payouts(ts, asset, payout)                          payout % snapshot, once per minute
"""
import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

from api_quotex.client import AsyncQuotexClient
from api_quotex.login import get_ssid

TF = 60  # one-minute candles only
DB_PATH = Path("data/candles.db")
HISTORY = 200


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS candles (
        asset TEXT NOT NULL, ts INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (asset, ts))""")
    db.execute("""CREATE TABLE IF NOT EXISTS payouts (
        ts INTEGER NOT NULL, asset TEXT NOT NULL, payout INTEGER,
        PRIMARY KEY (asset, ts))""")
    return db


class TickAggregator:
    """Turns [asset, ts, price, ...] ticks into closed 1-minute candles."""

    def __init__(self) -> None:
        self.buckets: Dict[str, list] = {}     # asset -> [minute, open, high, low, close, ticks]
        self.skip_minute: Dict[str, int] = {}  # first (possibly partial) minute seen per asset
        self.closed: List[Tuple] = []          # finished candles waiting to be written

    def reset(self) -> None:
        self.buckets.clear()
        self.skip_minute.clear()

    def on_quotes(self, data) -> None:
        if not isinstance(data, list):
            return
        for q in data:
            try:
                asset, ts, price = q[0], float(q[1]), float(q[2])
            except (TypeError, ValueError, IndexError):
                continue
            minute = int(ts) // TF * TF
            b = self.buckets.get(asset)
            if b is None:
                self.skip_minute[asset] = minute          # joined mid-minute: this one is incomplete
                self.buckets[asset] = [minute, price, price, price, price, 1]
            elif minute == b[0]:
                b[2] = max(b[2], price)
                b[3] = min(b[3], price)
                b[4] = price
                b[5] += 1
            elif minute > b[0]:
                if b[0] != self.skip_minute.get(asset):
                    self.closed.append((asset, b[0], b[1], b[2], b[3], b[4], b[5]))
                self.buckets[asset] = [minute, price, price, price, price, 1]
            # minute < b[0]: late/out-of-order tick, ignore

    def drain(self) -> List[Tuple]:
        out, self.closed = self.closed, []
        return out


def eligible_pairs(assets: dict, min_payout: int) -> Dict[str, int]:
    return {
        sym: info["payout"]
        for sym, info in assets.items()
        if info.get("is_open") and info.get("payout", 0) > min_payout and TF in info.get("available_timeframes", [])
    }


async def backfill(client, db, symbols: List[str]) -> int:
    """Insert the server's closed history candles (authoritative, so they replace tick-built rows)."""
    sem = asyncio.Semaphore(8)
    now = time.time()

    async def fetch(sym):
        async with sem:
            try:
                return sym, await client.get_fresh_candles(sym, TF, HISTORY)
            except Exception as e:
                print(f"  backfill {sym}: {e}")
                return sym, []

    n = 0
    results = await asyncio.gather(*(fetch(s) for s in symbols))
    with db:
        for sym, candles in results:
            # The history snapshot can be a few minutes old, so its newest candle is the one that was still
            # forming when the snapshot was taken (a handful of ticks). Drop it; the live feed covers it.
            rows = [
                (sym, int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close, c.volume)
                for c in candles[:-1] if c.timestamp.timestamp() + TF <= now
            ]
            db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)", rows)
            n += len(rows)
    return n


async def follow(client, symbols: List[str]) -> None:
    for sym in symbols:
        await client.send_message(f'42["instruments/follow","{sym}"]')
        await client.send_message(f'42["instruments/update",{{"asset":"{sym}","period":{TF}}}]')
        await asyncio.sleep(0.02)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-payout", type=int, default=80, help="collect pairs paying MORE than this percent (default 80)")
    ap.add_argument("--live", action="store_true", help="use the live account (default: demo)")
    args = ap.parse_args()

    ok, session = await get_ssid(is_demo=not args.live)
    if not ok:
        sys.exit("login failed")
    client = AsyncQuotexClient(ssid=session["ssid"], is_demo=not args.live)
    db = open_db()
    agg = TickAggregator()
    client.add_event_callback("quote_stream", agg.on_quotes)

    followed: Set[str] = set()
    seen_connections = -1
    last_minute = 0
    saved = 0
    try:
        while True:
            if not client.is_connected:
                try:
                    await client.connect()
                except Exception as e:
                    print(f"connect failed: {e}")
                await asyncio.sleep(5)
                continue

            conns = client.get_connection_stats().get("successful_connections", 0)
            if conns != seen_connections:             # (re)connected: subscriptions are gone, buckets are stale
                seen_connections = conns
                followed.clear()
                agg.reset()

            assets = await client.get_available_assets()
            pairs = eligible_pairs(assets, args.min_payout)
            new = [s for s in pairs if s not in followed]
            if new:
                # History first: the server won't re-send it for a pair that is already subscribed.
                n = await backfill(client, db, new)
                await follow(client, new)
                followed.update(new)
                print(f"{time.strftime('%H:%M:%S')}  following {len(new)} new pairs (total {len(followed)}), backfilled {n} candles", flush=True)

            rows = agg.drain()
            if rows:
                with db:
                    db.executemany("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)", rows)
                saved += len(rows)

            minute = int(time.time()) // 60 * 60
            if minute != last_minute and pairs:
                last_minute = minute
                with db:
                    db.executemany("INSERT OR REPLACE INTO payouts VALUES (?,?,?)", [(minute, s, p) for s, p in pairs.items()])
                total = db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
                print(f"{time.strftime('%H:%M:%S')}  following={len(followed)} eligible_now={len(pairs)} live_candles_saved={saved} db_rows={total}", flush=True)

            await asyncio.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        rows = agg.drain()
        if rows:
            with db:
                db.executemany("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)", rows)
        await client.disconnect()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
