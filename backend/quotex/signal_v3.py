"""
v3-reversion-plus: rule-based mean-reversion engine (no model, recomputed from the latest closes on every call).

Three oscillators (RSI, Bollinger %B, Stochastic RSI) are each turned into a score in [-1, +1] where
positive = oversold = expect a bounce up. They decide the call. Supertrend, EMA stack, MACD and the
Kaufman efficiency ratio are computed for the panel only and never decide anything.

Closes only: every indicator here uses closing prices.
"""

from typing import Dict, List, Optional

import numpy as np

from indicators import TechnicalIndicators

ENGINE = "v3-reversion-plus"
MIN_CANDLES = 60
MIN_SCORE = 0.25
WEIGHTS = {"rsi": 0.40, "bb": 0.35, "sr": 0.25}


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def _last_valid(values: List[float]) -> Optional[float]:
    return float(values[-1]) if len(values) and not np.isnan(values[-1]) else None


def components(c: np.ndarray) -> Dict[str, float]:
    """The three decision scores plus the raw readings. Positive = oversold = bullish."""
    rsi_series = np.array(TechnicalIndicators.calculate_rsi(c.tolist(), 14), dtype=float)
    rsi = _last_valid(rsi_series)
    rsi = 50.0 if rsi is None else rsi

    window = c[-20:]
    mean, sd = float(window.mean()), float(window.std())
    pct_b = 0.5 if sd == 0 else (float(c[-1]) - (mean - 2 * sd)) / (4 * sd)

    recent = rsi_series[-14:]
    recent = recent[~np.isnan(recent)]
    if len(recent) < 3 or recent.max() == recent.min():
        stoch_rsi = 0.5
    else:
        stoch_rsi = float((rsi - recent.min()) / (recent.max() - recent.min()))

    return {
        "rsi": rsi, "pct_b": pct_b, "stoch_rsi": stoch_rsi,
        "rsi_mr": clip((50 - rsi) / 25),
        "bb_mr": clip((0.5 - pct_b) * 2),
        "sr_mr": clip((0.5 - stoch_rsi) * 2),
    }


def weighted_score(comp: Dict[str, float]) -> float:
    return WEIGHTS["rsi"] * comp["rsi_mr"] + WEIGHTS["bb"] * comp["bb_mr"] + WEIGHTS["sr"] * comp["sr_mr"]


def snapshot(closes: List[float]) -> Optional[Dict]:
    """Live readings for the monitor panel: raw oscillators, signed score and whether a clean setup exists."""
    c = np.asarray(closes, dtype=float)
    if len(c) < MIN_CANDLES or not np.all(np.isfinite(c)):
        return None
    comp = components(c)
    return {
        "rsi": round(comp["rsi"], 1), "pct_b": round(comp["pct_b"], 3), "stoch_rsi": round(comp["stoch_rsi"], 3),
        "score": round(weighted_score(comp), 3), "setup": reversion_plus(comp) is not None,
    }


def series(closes: List[float], n: int = 40) -> Optional[Dict[str, List[float]]]:
    """Last n closes with the Bollinger bands and RSI at each point, for drawing the monitor chart."""
    c = np.asarray(closes, dtype=float)
    if len(c) < MIN_CANDLES or not np.all(np.isfinite(c)):
        return None
    rsi = np.array(TechnicalIndicators.calculate_rsi(c.tolist(), 14), dtype=float)
    idx = range(len(c) - n, len(c))
    upper, lower = [], []
    for i in idx:
        w = c[i - 19:i + 1]
        m, s = float(w.mean()), float(w.std())
        upper.append(round(m + 2 * s, 6))
        lower.append(round(m - 2 * s, 6))
    return {
        "closes": [round(float(c[i]), 6) for i in idx],
        "rsi": [round(float(rsi[i]), 2) for i in idx],
        "upper": upper,
        "lower": lower,
    }


def reversion_plus(comp: Dict[str, float], min_score: float = MIN_SCORE) -> Optional[Dict[str, float]]:
    """Weighted score and direction (+1 CALL, -1 PUT), or None when the setup is not clean enough."""
    parts = (comp["rsi_mr"], comp["bb_mr"], comp["sr_mr"])
    score = weighted_score(comp)
    d = 1 if score > 0 else -1
    if abs(score) < min_score:
        return None
    if any(p == 0 or (p > 0) != (d > 0) for p in parts):
        return None
    return {"score": score, "direction": d}


# ---------------------------------------------------------------- context only (never decides)
def supertrend(c: np.ndarray, period: int = 10, mult: float = 2.0) -> Dict[str, float]:
    """Close-only Supertrend: ATR is the Wilder average of abs(close - previous close)."""
    tr = np.abs(np.diff(c, prepend=c[0]))
    atr = np.full(len(c), np.nan)
    atr[period] = tr[1:period + 1].mean()
    for i in range(period + 1, len(c)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    trend, age = 1, 0
    final_upper = c[period] + mult * atr[period]
    final_lower = c[period] - mult * atr[period]
    for i in range(period + 1, len(c)):
        upper, lower = c[i] + mult * atr[i], c[i] - mult * atr[i]
        final_upper = upper if upper < final_upper or c[i - 1] > final_upper else final_upper
        final_lower = lower if lower > final_lower or c[i - 1] < final_lower else final_lower
        if trend == -1 and c[i] > final_upper:
            trend, age = 1, 0
        elif trend == 1 and c[i] < final_lower:
            trend, age = -1, 0
        else:
            age += 1
    line = final_lower if trend == 1 else final_upper
    dist = abs(c[-1] - line) / max(atr[-1], 1e-12)
    score = trend * clip(0.6 + 0.2 * min(dist, 2) - 0.02 * max(age - 5, 0), 0.2, 1.0)
    return {"dir": trend, "dist": float(dist), "age": age, "score": float(score)}


def ema_stack(c: np.ndarray) -> float:
    e9 = _last_valid(TechnicalIndicators.calculate_ema(c.tolist(), 9))
    e21 = _last_valid(TechnicalIndicators.calculate_ema(c.tolist(), 21))
    e50 = _last_valid(TechnicalIndicators.calculate_ema(c.tolist(), 50))
    if e9 is None or e21 is None or e50 is None:
        return 0.0
    price = float(c[-1])
    scale = max(price * 0.0004, 1e-12)
    return 0.7 * clip((e9 - e21) / scale) + 0.3 * clip((price - e50) / (2 * scale))


def macd_score(c: np.ndarray) -> float:
    _, _, hist = TechnicalIndicators.calculate_macd(c.tolist())
    hist = np.array(hist, dtype=float)
    if len(hist) < 2 or np.isnan(hist[-1]) or np.isnan(hist[-2]):
        return 0.0
    valid = hist[-30:][~np.isnan(hist[-30:])]
    h_scale = max(float(np.abs(valid).max()), 1e-12)
    h, h_prev = float(hist[-1]), float(hist[-2])
    return 0.6 * clip(h / h_scale) + 0.4 * clip((h - h_prev) / h_scale * 2)


def efficiency_ratio(c: np.ndarray, period: int = 10) -> float:
    path = np.abs(np.diff(c[-(period + 1):])).sum()
    return 0.0 if path == 0 else float(abs(c[-1] - c[-(period + 1)]) / path)


def analyze(closes: List[float]) -> Optional[Dict]:
    """Full v3-reversion-plus result for one pair, or None when there is no signal."""
    c = np.asarray(closes, dtype=float)
    if len(c) < MIN_CANDLES or not np.all(np.isfinite(c)):
        return None
    comp = components(c)
    decision = reversion_plus(comp)
    if decision is None:
        return None
    return {
        "direction": decision["direction"],
        "score": decision["score"],
        "components": comp,
        "supertrend": supertrend(c),
        "ema_stack": ema_stack(c),
        "macd": macd_score(c),
        "ker": efficiency_ratio(c),
    }
