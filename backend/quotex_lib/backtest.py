"""Honest out-of-sample evaluation of every strategy in signals.py on the collected 1-minute candles.

    python backtest.py

Rule-based strategies have fixed parameters, so they are scored on a TRAIN period (first 60% of the time
range) and a later TEST period (last 40%); only the test numbers count. The ML models are walk-forward:
each fold is trained strictly on the past and scored on the next, unseen slice of time.

A trade wins if the next 1-minute candle closes in the predicted direction. Payout is the pair's real
payout, so ROI is per $1 staked. "p" is the one-sided binomial probability of seeing at least this many wins
by luck if the true win rate were exactly the break-even rate. With dozens of strategies tested, only
p below 0.05 / (number tested) is convincing.
"""
import warnings

import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from signals import FEATURES, RULES, load_candles, load_payouts, make_dataset

warnings.filterwarnings("ignore")
DEFAULT_PAYOUT = 0.85


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


def score(win: np.ndarray, payout: np.ndarray) -> dict:
    n = len(win)
    if n == 0:
        return dict(n=0, wr=np.nan, lo=np.nan, hi=np.nan, be=np.nan, roi=np.nan, p=np.nan)
    k = int(win.sum())
    be = float(np.mean(1 / (1 + payout)))
    lo, hi = wilson(k, n)
    return dict(n=n, wr=k / n, lo=lo, hi=hi, be=be,
                roi=float(np.mean(np.where(win, payout, -1.0))),
                p=binomtest(k, n, be, alternative="greater").pvalue)


def fmt(s: dict) -> str:
    if s["n"] == 0:
        return "    0 trades"
    return (f"{s['n']:5d} trades  win {100 * s['wr']:5.1f}% (95% CI {100 * s['lo']:4.1f}-{100 * s['hi']:4.1f})  "
            f"break-even {100 * s['be']:4.1f}%  ROI {100 * s['roi']:+6.1f}%  p={s['p']:.3f}")


def main() -> None:
    candles = load_candles()
    payouts = load_payouts()
    df = make_dataset(candles)
    df["payout"] = df["asset"].map(payouts).fillna(DEFAULT_PAYOUT)
    span = (df.index.max() - df.index.min()).total_seconds() / 3600
    print(f"Dataset: {len(df)} labelled candles, {df['asset'].nunique()} pairs, {span:.1f} h of history "
          f"({df.index.min():%m-%d %H:%M} -> {df.index.max():%m-%d %H:%M} UTC)")
    print(f"Average payout {100 * df['payout'].mean():.0f}%  ->  need to win {100 * np.mean(1 / (1 + df['payout'])):.1f}% to break even")
    print(f"Up/down balance of next candle: {100 * (df['next_dir'] == 1).mean():.1f}% up\n")

    t_split = df.index.min() + 0.6 * (df.index.max() - df.index.min())
    train, test = df[df.index < t_split], df[df.index >= t_split]

    # ------------------------------------------------------------------ rule-based strategies
    print("=== Rule-based strategies (fixed parameters; TEST period is the only one that counts) ===")
    rows = []
    for name in RULES:
        col = f"sig_{name}"
        res = {}
        for part_name, part in (("train", train), ("test", test)):
            m = part[col] != 0
            win = (part.loc[m, col] == part.loc[m, "next_dir"]).to_numpy()
            res[part_name] = score(win, part.loc[m, "payout"].to_numpy())
        rows.append((name, res))
    rows.sort(key=lambda r: -(r[1]["test"]["roi"] if r[1]["test"]["n"] else -9))
    for name, res in rows:
        print(f"{name:26s} train: win {100 * res['train']['wr']:5.1f}% ({res['train']['n']:4d})   TEST: {fmt(res['test'])}")
    n_tests = len(RULES)
    alpha = 0.05 / n_tests
    winners = [n for n, r in rows if r["test"]["n"] >= 100 and r["test"]["p"] < alpha and r["test"]["roi"] > 0]
    print(f"\nSignificance bar after testing {n_tests} strategies: p < {alpha:.4f}")
    print("Strategies clearing it:", winners if winners else "none")

    # ------------------------------------------------------------------ ML walk-forward
    print("\n=== Machine learning, walk-forward (train on past only, score on the next unseen slice) ===")
    tsn = (df.index - pd.Timestamp("1970-01-01", tz="UTC")).total_seconds().to_numpy()   # epoch seconds
    qs = np.quantile(tsn, [0.4, 0.55, 0.7, 0.85, 1.0])
    X = df[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy()
    y = (df["next_dir"] == 1).astype(int).to_numpy()
    embargo = 120.0   # seconds kept between training rows and the test slice
    models = {
        "logistic": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.05, max_iter=500)),
        "grad_boost": lambda: HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=5.0, min_samples_leaf=50, random_state=0),
    }
    n_ml = 0
    ml_hits = []
    for mname, make in models.items():
        proba = np.full(len(df), np.nan)
        start = qs[0]
        for end in qs[1:]:
            tr = tsn < start - embargo
            te = (tsn >= start) & (tsn <= end if end == qs[-1] else tsn < end)
            if tr.sum() < 500 or te.sum() == 0:
                start = end
                continue
            model = make().fit(X[tr], y[tr])
            proba[te] = model.predict_proba(X[te])[:, 1]
            start = end
        ok = ~np.isnan(proba)
        pay = df["payout"].to_numpy()
        acc = ((proba[ok] > 0.5) == (y[ok] == 1)).mean()
        base = max(y[ok].mean(), 1 - y[ok].mean())
        print(f"\n{mname}: scored on {ok.sum()} unseen candles, directional accuracy {100 * acc:.1f}% "
              f"(always-guess-majority baseline {100 * base:.1f}%)")
        for conf in (0.50, 0.52, 0.54, 0.56, 0.58, 0.60):
            sel = ok & (np.maximum(proba, 1 - proba) >= conf)
            win = ((proba[sel] > 0.5) == (y[sel] == 1))
            s = score(win, pay[sel])
            n_ml += 1
            print(f"  confidence >= {conf:.2f}: {fmt(s)}")
            if s["n"] >= 100 and s["roi"] > 0:
                ml_hits.append((mname, conf, s))
    alpha_ml = 0.05 / n_ml
    sig_ml = [(m, c) for m, c, s in ml_hits if s["p"] < alpha_ml]
    print(f"\nSignificance bar after {n_ml} ML cuts: p < {alpha_ml:.4f}.  ML cuts clearing it: {sig_ml if sig_ml else 'none'}")

    print("\n=== Verdict ===")
    if winners or sig_ml:
        print("At least one strategy beat break-even by more than luck allows on unseen data. Re-run after more data "
              "has been collected before trusting it with anything beyond demo.")
    else:
        print("No strategy shows an edge over break-even that is distinguishable from luck on this data.")
    print(f"(Only {span:.1f} hours of history so far: results this small can hide a modest edge. "
          "Keep collector.py running and re-run.)")


if __name__ == "__main__":
    main()
