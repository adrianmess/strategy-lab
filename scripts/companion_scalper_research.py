#!/usr/bin/env python3
"""RESEARCH: a COMPANION instance that scalps the chop while another
instance holds a position.

Instance A (existing) holds its core trade for hours/days waiting for its
target. Instance B (this idea) has its OWN capital on a DIFFERENT account —
so it may trade either direction on the same symbol — and fades the
oscillations around A's entry price: price stretches a band away, B bets on
the snap back, closes at the reference, repeats until A's trade ends.

The question that decides everything is not "is the scalper profitable?"
but "is it profitable BECAUSE A is holding?". So every configuration is run
twice:
    WHILE-HELD : only inside A's hold windows (the proposal)
    CONTROL    : the same rules over ALL bars (the null hypothesis)
If CONTROL matches WHILE-HELD, A's position carries no information and this
is just a standalone mean-reversion strategy that needs its own research.

Rails: 1m bars, entries/exits on bar extremes with ONE action per bar,
taker fees both sides, a stop, and a TRAIN/HOLDOUT split.

  python3 companion_scalper_research.py --config <A's component config>
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "optimizer"))
DATA = os.path.join(REPO, "adaptive_trader", "research", "data")


def scalp(bars, ref, band, stop_mult, comm, lev):
    """Fade deviations from `ref` on 1m bars. Returns list of trade returns
    (fraction of the companion's equity, leverage applied).
    Long when price dips a band below ref, short when it pokes a band above;
    take profit back at ref, stop at stop_mult bands beyond entry."""
    out = []
    pos = 0            # 0 flat, +1 long, -1 short
    entry = 0.0
    armed = True       # re-arm rule: after a close, price must come back
    #                    INSIDE the band before we may fade it again —
    #                    without this the scalper re-enters every bar while
    #                    price sits beyond the band and churns itself to death
    hi = bars["high"].to_numpy()
    lo = bars["low"].to_numpy()
    for h, l in zip(hi, lo):
        if pos == 0:
            if not armed:
                if ref * (1 - band) < l and h < ref * (1 + band):
                    armed = True
                continue
            if l <= ref * (1 - band):          # dip -> fade long
                pos, entry = 1, ref * (1 - band)
            elif h >= ref * (1 + band):        # pop -> fade short
                pos, entry = -1, ref * (1 + band)
            continue                            # one action per bar
        if pos > 0:
            if h >= ref:                        # snapped back
                out.append(lev * ((ref / entry - 1) - 2 * comm))
                pos, armed = 0, False
            elif l <= entry * (1 - stop_mult * band):
                out.append(lev * ((entry * (1 - stop_mult * band) / entry - 1)
                                  - 2 * comm))
                pos, armed = 0, False
        else:
            if l <= ref:
                out.append(lev * ((entry / ref - 1) - 2 * comm))
                pos, armed = 0, False
            elif h >= entry * (1 + stop_mult * band):
                out.append(lev * ((entry / (entry * (1 + stop_mult * band)) - 1)
                                  - 2 * comm))
                pos, armed = 0, False
    return out


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + max(r, -0.999))
    return eq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--split", default="2026-01-01")
    ap.add_argument("--bands", default="0.003,0.005,0.008,0.012")
    ap.add_argument("--stop", type=float, default=3.0)
    ap.add_argument("--lev", type=float, default=1.0)
    ap.add_argument("--window-hours", type=float, default=6.0,
                    help="CONTROL: length of the synthetic windows used to "
                         "mimic hold windows over all time")
    a = ap.parse_args()

    import backtest_cli as BT
    cfg = json.load(open(a.config))
    pair, mode = cfg.get("pair", "SOL_USDT"), cfg.get("mode", "lev")
    comm = 0.0004 if mode == "lev" else 0.0005
    e = BT.run_single(a.config)
    holds = []
    for t in e["trades"]:
        try:
            holds.append((pd.Timestamp(t["entry_t"]), pd.Timestamp(t["exit_t"]),
                          float(t["entry"])))
        except Exception:
            pass
    coin = pair.split("_")[0].lower()
    suffix = "_spot" if cfg.get("market_data") == "spot" else ""
    d1 = pd.read_parquet(os.path.join(DATA, f"{coin}{suffix}_1min.parquet"))
    d1 = d1.rename(columns=str.lower)
    d1["t"] = pd.to_datetime(d1["t"]).dt.tz_localize(None)
    split = pd.Timestamp(a.split)

    print(f"companion scalper on {pair} ({mode}) while "
          f"{os.path.basename(os.path.dirname(a.config))} holds")
    print(f"{len(holds)} hold windows | companion leverage {a.lev:g}x, "
          f"stop {a.stop:g} bands, fees {100*comm:.2f}%/side\n")
    print("per-TRADE expectancy (fees paid) — the fair comparison, since the "
          "control has far more windows\n")
    print(f"{'band':>6} | {'HELD train':>10} {'HELD hold':>10} {'n':>6} "
          f"{'win':>5} | {'CTRL train':>10} {'CTRL hold':>10} {'n':>7} "
          f"{'win':>5}")

    # control windows: same average length, tiled across the whole series
    wl = pd.Timedelta(hours=a.window_hours)
    t0, t1 = d1["t"].iloc[0], d1["t"].iloc[-1]
    ctrl = []
    t = t0
    while t < t1:
        w = d1[(d1["t"] >= t) & (d1["t"] < t + wl)]
        if len(w) > 10:
            ctrl.append((t, t + wl, float(w["close"].iloc[0])))
        t += wl

    for band in [float(b) for b in a.bands.split(",")]:
        res = {}
        for label, wins in (("held", holds), ("ctrl", ctrl)):
            tr_r, ho_r = [], []
            for (s, en, ref) in wins:
                w = d1[(d1["t"] >= s) & (d1["t"] <= en)]
                if len(w) < 10:
                    continue
                r = scalp(w, ref, band, a.stop, comm, a.lev)
                (tr_r if en <= split else ho_r).extend(r)
            allr = tr_r + ho_r
            res[label] = (float(np.mean(ho_r)) if ho_r else 0.0, len(ho_r),
                          float(np.mean(tr_r)) if tr_r else 0.0, len(tr_r),
                          float(np.mean([1 for r in allr if r > 0]) if allr
                                else 0) and
                          sum(1 for r in allr if r > 0) / max(len(allr), 1))
        h, c = res["held"], res["ctrl"]
        print(f"{100*band:>5.2f}% | {100*h[2]:>+9.3f}% {100*h[0]:>+9.3f}% "
              f"{h[1]+h[3]:>6} {100*h[4]:>5.0f}% | {100*c[2]:>+9.3f}% "
              f"{100*c[0]:>+9.3f}% {c[1]+c[3]:>7} {100*c[4]:>5.0f}%")


if __name__ == "__main__":
    main()
