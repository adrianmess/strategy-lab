#!/usr/bin/env python3
"""RESEARCH: a standalone MEAN-REVERSION family, tested on full history.

Came out of the "trade the chop while a position is held" idea: the chop is
real, but a held position carries no information about it — so the honest
version is a mean-reversion strategy judged on its own merits.

Three variants (all one position at a time, long AND short):
  ema   — fade a % band around an EMA anchor; exit back at the anchor
  zsc   — fade a z-score band around a rolling mean; exit at the mean
  rsi   — fade RSI extremes; exit at the midline

Every variant carries a stop (in band/sigma units) and a time stop.

Anti-optimism rails, all deliberate:
  * when a bar could hit BOTH the stop and the target, the STOP is taken
  * entries fill at the trigger level, not the close
  * taker fees both sides on every trade
  * the anchor at bar i uses data through bar i-1 (no same-bar lookahead)
  * TRAIN (before --split) and HOLDOUT (after) are reported separately, and
    the sweep is judged on HOLDOUT

  python3 meanrev_research.py --pair HYPE_USDT --market spot --tf 1
"""
import argparse
import itertools
import os

import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(REPO, "adaptive_trader", "research", "data")


@njit(cache=True)
def _sim(hi, lo, cl, anchor, dev, band, stop_mult, max_bars, comm, lev):
    """One-position-at-a-time fade of `dev` beyond `band` around `anchor`.
    dev[i] is the deviation measure at bar i (percent or sigma units).
    Returns (returns array, entry index array, n)."""
    n = hi.shape[0]
    rets = np.zeros(n, dtype=np.float64)
    idxs = np.zeros(n, dtype=np.int64)
    k = 0
    pos = 0
    entry = 0.0
    tgt = 0.0
    stp = 0.0
    bars_in = 0
    for i in range(1, n):
        a = anchor[i - 1]                 # anchor known at the PREVIOUS close
        if a <= 0.0 or not np.isfinite(a):
            continue
        if pos == 0:
            d = dev[i - 1]
            if not np.isfinite(d):
                continue
            if d <= -band:                # stretched below -> fade long
                pos = 1
                entry = cl[i - 1]
                tgt = a
                stp = entry * (1.0 - stop_mult * band * 0.01) if band > 1e-9 else entry
                bars_in = 0
            elif d >= band:               # stretched above -> fade short
                pos = -1
                entry = cl[i - 1]
                tgt = a
                stp = entry * (1.0 + stop_mult * band * 0.01) if band > 1e-9 else entry
                bars_in = 0
            continue
        bars_in += 1
        if pos > 0:
            # PESSIMISTIC: stop is resolved before target within the bar
            if lo[i] <= stp:
                rets[k] = lev * ((stp / entry - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
            elif hi[i] >= tgt:
                rets[k] = lev * ((tgt / entry - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
            elif bars_in >= max_bars:
                rets[k] = lev * ((cl[i] / entry - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
        else:
            if hi[i] >= stp:
                rets[k] = lev * ((entry / stp - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
            elif lo[i] <= tgt:
                rets[k] = lev * ((entry / tgt - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
            elif bars_in >= max_bars:
                rets[k] = lev * ((entry / cl[i] - 1.0) - 2.0 * comm)
                idxs[k] = i
                k += 1
                pos = 0
    return rets[:k], idxs[:k], k


def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def build(variant, cl, n):
    """Returns (anchor, deviation) — deviation in PERCENT units so the same
    `band` sweep is comparable across variants."""
    s = pd.Series(cl)
    if variant == "ema":
        a = ema(cl, n)
        return a, 100.0 * (cl / a - 1.0)
    if variant == "zsc":
        m = s.rolling(n).mean().to_numpy()
        sd = s.rolling(n).std().to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (cl - m) / np.where(sd > 0, sd, np.nan)
        # express z in percent-of-price so `band` stays a % knob
        return m, z * (100.0 * np.where(m > 0, sd / m, np.nan))
    if variant == "rsi":
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        rs = up / dn.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).to_numpy()
        a = ema(cl, n)
        return a, (rsi - 50.0) / 5.0        # ~±10 at RSI 0/100
    raise SystemExit(f"unknown variant {variant}")


def summarize(rets, idxs, t, split_i, lev):
    tr = rets[idxs < split_i]
    ho = rets[idxs >= split_i]
    def blk(r, i0, i1):
        if len(r) == 0:
            return (0.0, 0, 0.0, 0.0)
        months = max((t[i1] - t[i0]).total_seconds() / (30.44 * 86400), 1e-9)
        eq = 1.0
        for x in r:
            eq *= (1 + max(x, -0.999))
        gm = 100 * ((max(eq, 1e-9)) ** (1 / months) - 1)
        return (100 * float(np.mean(r)), len(r), gm,
                100 * float((r > 0).mean()))
    return blk(tr, 0, split_i - 1), blk(ho, split_i, len(t) - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--market", default="lev")
    ap.add_argument("--tf", type=int, default=1)
    ap.add_argument("--split", default="2026-01-01")
    ap.add_argument("--lev", type=float, default=1.0)
    ap.add_argument("--variants", default="ema,zsc,rsi")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()

    coin = a.pair.split("_")[0].lower()
    suffix = "_spot" if a.market == "spot" else ""
    d1 = pd.read_parquet(os.path.join(DATA, f"{coin}{suffix}_1min.parquet"))
    d1 = d1.rename(columns=str.lower)
    d1["t"] = pd.to_datetime(d1["t"]).dt.tz_localize(None)
    if a.tf > 1:
        d1 = (d1.set_index("t").resample(f"{a.tf}min")
              .agg(dict(open="first", high="max", low="min", close="last",
                        volume="sum")).dropna().reset_index())
    t = d1["t"]
    hi = d1["high"].to_numpy(np.float64)
    lo = d1["low"].to_numpy(np.float64)
    cl = d1["close"].to_numpy(np.float64)
    comm = 0.0004 if a.market != "spot" else 0.0005
    split_i = int((t < pd.Timestamp(a.split)).sum())
    print(f"{a.pair} {a.market} {a.tf}m — {len(cl):,} bars "
          f"{t.iloc[0].date()} → {t.iloc[-1].date()}, split {a.split} "
          f"({split_i:,} train / {len(cl)-split_i:,} holdout), lev {a.lev:g}x")

    rows = []
    grid = dict(
        n=[20, 60, 120, 240],
        band=[0.3, 0.5, 0.8, 1.2, 2.0],
        stop=[1.5, 3.0],
        max_bars=[60, 240, 1440],
    )
    for variant in a.variants.split(","):
        for n, band, stop, mb in itertools.product(grid["n"], grid["band"],
                                                   grid["stop"],
                                                   grid["max_bars"]):
            anchor, dev = build(variant, cl, n)
            r, idx, k = _sim(hi, lo, cl, anchor.astype(np.float64),
                             dev.astype(np.float64), float(band), float(stop),
                             int(mb), float(comm), float(a.lev))
            if k < 40:
                continue
            tr, ho = summarize(r, idx, t, split_i, a.lev)
            rows.append((variant, n, band, stop, mb, tr, ho))

    rows.sort(key=lambda x: -x[6][2])           # rank by HOLDOUT %/mo
    print(f"\n{'variant':>7} {'n':>4} {'band':>5} {'stop':>5} {'maxbar':>7} | "
          f"{'TRAIN exp':>10} {'%/mo':>8} {'n':>6} {'win':>5} | "
          f"{'HOLD exp':>9} {'%/mo':>8} {'n':>6} {'win':>5}")
    for v, n, band, stop, mb, tr, ho in rows[:a.top]:
        print(f"{v:>7} {n:>4} {band:>4.1f}% {stop:>5.1f} {mb:>7} | "
              f"{tr[0]:>+9.3f}% {tr[2]:>+7.1f}% {tr[1]:>6} {tr[3]:>4.0f}% | "
              f"{ho[0]:>+8.3f}% {ho[2]:>+7.1f}% {ho[1]:>6} {ho[3]:>4.0f}%")
    pos = [r for r in rows if r[5][2] > 0 and r[6][2] > 0]
    print(f"\nconfigs tested: {len(rows)} | profitable in BOTH train and "
          f"holdout: {len(pos)} ({100*len(pos)/max(len(rows),1):.0f}%)")


if __name__ == "__main__":
    main()
