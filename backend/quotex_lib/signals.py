"""Indicators, features and rule-based strategies for 1-minute direction prediction.

Everything here is computed from candles that have already CLOSED, so a signal on row t uses data up to
the close of candle t only. The trade it describes is: enter at the close of candle t (== open of t+1),
expire one minute later. Label for row t: did close[t+1] finish above close[t] (CALL wins) or below (PUT wins).

Signals are +1 (CALL), -1 (PUT) or 0 (no trade).
"""
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List

import numpy as np
import pandas as pd

DB_PATH = Path("data/candles.db")


# ----------------------------------------------------------------------------- data loading
def load_candles(db_path: Path = DB_PATH) -> Dict[str, pd.DataFrame]:
    """{asset: DataFrame(open, high, low, close, volume)} indexed by UTC minute, oldest first."""
    db = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT asset, ts, open, high, low, close, volume FROM candles ORDER BY asset, ts", db)
    db.close()
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return {a: g.drop(columns="asset").set_index("ts") for a, g in df.groupby("asset")}


def load_payouts(db_path: Path = DB_PATH) -> Dict[str, float]:
    """Latest known payout (as a fraction, e.g. 0.87) per asset."""
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT asset, payout FROM payouts WHERE (asset, ts) IN "
                      "(SELECT asset, MAX(ts) FROM payouts GROUP BY asset)").fetchall()
    db.close()
    return {a: p / 100.0 for a, p in rows}


def segments(df: pd.DataFrame, min_len: int) -> List[pd.DataFrame]:
    """Split a candle series wherever a minute is missing, so indicators never straddle a gap."""
    broken = df.index.to_series().diff() > pd.Timedelta(seconds=60)
    parts = [g for _, g in df.groupby(broken.cumsum().values)]
    return [g for g in parts if len(g) >= min_len]


# ----------------------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def streak(color: pd.Series) -> pd.Series:
    """Signed run length of same-colour candles (+3 = three green in a row), capped at +-6."""
    grp = (color != color.shift()).cumsum()
    run = color.groupby(grp).cumcount() + 1
    return (run * color).clip(-6, 6)


def feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Indicator/feature columns for one contiguous candle segment. Scale-free so pairs can be pooled."""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    a = atr(df).replace(0, np.nan)
    rng = (h - l).replace(0, np.nan)
    f = pd.DataFrame(index=df.index)

    ret = c.pct_change()
    vol = ret.rolling(20).std().replace(0, np.nan)
    for k in range(1, 6):
        f[f"zret{k}"] = ret.shift(k - 1) / vol

    f["rsi14"] = rsi(c, 14)
    f["rsi7"] = rsi(c, 7)

    ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    f["pctb"] = (c - (ma20 - 2 * sd20)) / (4 * sd20).replace(0, np.nan)
    f["bb_width"] = 4 * sd20 / ma20

    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    f["stoch_k"] = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)
    f["stoch_d"] = f["stoch_k"].rolling(3).mean()

    macd = ema(c, 12) - ema(c, 26)
    f["macd_hist"] = (macd - ema(macd, 9)) / a
    f["ema_diff"] = (ema(c, 9) - ema(c, 21)) / a
    f["dist_ema21"] = (c - ema(c, 21)) / a

    color = np.sign(c - o).astype(int)
    f["color"] = color
    f["body"] = (c - o).abs() / rng
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    f["lower_wick"] = (np.minimum(o, c) - l) / rng
    f["streak"] = streak(color)
    f["tick_ratio"] = v / v.rolling(20).mean().replace(0, np.nan)

    prev_o, prev_c = o.shift(), c.shift()
    f["bull_engulf"] = ((prev_c < prev_o) & (c > o) & (c >= prev_o) & (o <= prev_c)).astype(int)
    f["bear_engulf"] = ((prev_c > prev_o) & (c < o) & (c <= prev_o) & (o >= prev_c)).astype(int)
    return f


FEATURES = [
    "zret1", "zret2", "zret3", "zret4", "zret5", "rsi14", "rsi7", "pctb", "bb_width", "stoch_k", "stoch_d",
    "macd_hist", "ema_diff", "dist_ema21", "color", "body", "upper_wick", "lower_wick", "streak", "tick_ratio",
]


# ----------------------------------------------------------------------------- rule-based strategies
def _sig(buy: pd.Series, sell: pd.Series) -> pd.Series:
    return buy.astype(int) - sell.astype(int)


RULES: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "last_candle_momentum": lambda f: f["color"],
    "last_candle_reversal": lambda f: -f["color"],
    "rsi14_30_70_reversion": lambda f: _sig(f["rsi14"] < 30, f["rsi14"] > 70),
    "rsi7_20_80_reversion": lambda f: _sig(f["rsi7"] < 20, f["rsi7"] > 80),
    "rsi14_50_trend": lambda f: _sig(f["rsi14"] > 55, f["rsi14"] < 45),
    "bollinger_reversion": lambda f: _sig(f["pctb"] < 0, f["pctb"] > 1),
    "bollinger_breakout": lambda f: _sig(f["pctb"] > 1, f["pctb"] < 0),
    "stochastic_reversion": lambda f: _sig((f["stoch_k"] < 20) & (f["stoch_k"] > f["stoch_d"]),
                                           (f["stoch_k"] > 80) & (f["stoch_k"] < f["stoch_d"])),
    "ema9_21_trend": lambda f: np.sign(f["ema_diff"]).fillna(0).astype(int),
    "macd_hist_trend": lambda f: np.sign(f["macd_hist"]).fillna(0).astype(int),
    "macd_cross": lambda f: _sig((f["macd_hist"] > 0) & (f["macd_hist"].shift() <= 0),
                                 (f["macd_hist"] < 0) & (f["macd_hist"].shift() >= 0)),
    "streak3_reversal": lambda f: _sig(f["streak"] <= -3, f["streak"] >= 3),
    "streak3_momentum": lambda f: _sig(f["streak"] >= 3, f["streak"] <= -3),
    "engulfing_momentum": lambda f: _sig(f["bull_engulf"] == 1, f["bear_engulf"] == 1),
    "engulfing_reversal": lambda f: _sig(f["bear_engulf"] == 1, f["bull_engulf"] == 1),
    "wick_rejection": lambda f: _sig((f["lower_wick"] > 0.6) & (f["body"] < 0.3),
                                     (f["upper_wick"] > 0.6) & (f["body"] < 0.3)),
    "confluence_reversion": lambda f: _sig((f["rsi14"] < 35) & (f["pctb"] < 0.1) & (f["stoch_k"] < 25),
                                           (f["rsi14"] > 65) & (f["pctb"] > 0.9) & (f["stoch_k"] > 75)),
}


# ----------------------------------------------------------------------------- dataset
def make_dataset(candles: Dict[str, pd.DataFrame], min_len: int = 45, with_label: bool = True) -> pd.DataFrame:
    """One row per (asset, candle close): features, every rule's signal, and (optionally) the outcome.

    next_dir = +1 if the NEXT candle closed above this close, -1 if below, 0 if unchanged / unknown.
    Rows without a full indicator warm-up are dropped. With with_label=False the final candle of each
    segment is kept (that is the row you want to predict live).
    """
    parts = []
    for asset, df in candles.items():
        for seg in segments(df, min_len):
            f = feature_frame(seg)
            for name, rule in RULES.items():
                f[f"sig_{name}"] = rule(f).fillna(0).astype(int)
            f["next_dir"] = np.sign(seg["close"].shift(-1) - seg["close"]).fillna(0).astype(int)
            f["has_next"] = seg["close"].shift(-1).notna()
            f["asset"] = asset
            f = f.iloc[30:]                                       # indicator warm-up
            f = f.dropna(subset=FEATURES)
            if with_label:
                f = f[f["has_next"] & (f["next_dir"] != 0)]
            parts.append(f)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts).sort_index()
