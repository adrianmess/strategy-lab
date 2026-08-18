#!/usr/bin/env python3
"""RESEARCH: can the chop INSIDE a held position be harvested?

While a component holds a position for hours/days, price oscillates without
reaching the exit target. This tests the classic answer — trade a GRID
around the core: scale OUT a slice each time price moves a band in your
favour, scale back IN when it gives the band back. Each completed round trip
banks ~band% on the slice; the core keeps running for its own target.

IMPORTANT (why it's modelled as scale-out/in, not a hedge): MEXC futures
one-way mode cannot hold a long and a short on the same symbol — an
"opposite" order just reduces the position. So the only honest overlay on
the SAME pair is trimming and re-adding the core.

Honesty rails:
  * every fill pays taker fees (lev 0.04%, spot 0.05%) both ways
  * bands are checked against 1-MINUTE bars (high/low), not closes
  * results are split TRAIN (before --split) vs HOLDOUT (after). A band that
    only works in train is curve-fitting, and the sweep will show it.

  python3 overlay_grid_research.py --config <path to component config> \
      [--split 2026-01-01] [--slice 0.25]
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


def hold_intervals(cfg_path):
    """Component's historical trades: (entry_ts, exit_ts, dir, entry_px)."""
    import backtest_cli as BT
    e = BT.run_single(cfg_path)
    out = []
    for t in e["trades"]:
        try:
            out.append((pd.Timestamp(t["entry_t"]), pd.Timestamp(t["exit_t"]),
                        1 if t.get("dir", "long") == "long" else -1,
                        float(t["entry"])))
        except Exception:
            continue
    return out, e


def grid_overlay(d1, holds, band, slice_frac, comm, cap=None):
    """Walk 1m bars inside each hold. Long core: price up a band -> trim a
    slice (sell), price back down a band -> re-add (buy). Short core: mirror.
    Returns (extra return as a fraction of CORE notional, n round trips).
    `cap` limits how many slices may be trimmed at once (grid depth).

    Two anti-optimism rules, both learned the hard way:
      1. ONE action per bar. Within a single 1m bar we don't know whether the
         high or the low came first, so trimming at the high and re-adding at
         the low of the SAME bar is an unearnable sequence.
      2. Trims still open when the core exits are settled AT THE EXIT PRICE.
         If price runs away after a trim, that slice missed the rest of the
         move — the real cost of trading around a winner."""
    total, trips = 0.0, 0
    for (et, xt, dirn, ep) in holds:
        w = d1[(d1["t"] >= et) & (d1["t"] <= xt)]
        if len(w) < 5:
            continue
        ref = ep
        stack = []                      # prices at which slices were trimmed
        for hi, lo in zip(w["high"].to_numpy(), w["low"].to_numpy()):
            fav = hi if dirn > 0 else lo
            adv = lo if dirn > 0 else hi
            moved_fav = (fav / ref - 1.0) * dirn
            moved_adv = (adv / ref - 1.0) * dirn
            if moved_fav >= band and (cap is None or len(stack) < cap):
                ref = ref * (1 + band * dirn)
                stack.append(ref)
                continue                # rule 1: one action per bar
            if stack and moved_adv <= -band:
                ref = ref * (1 - band * dirn)
                stack.pop()
                trips += 1
                total += slice_frac * (band - 2 * comm)
        # rule 2: settle slices still trimmed when the core exits
        exit_px = float(w["close"].iloc[-1])
        for tp in stack:
            total -= slice_frac * ((exit_px / tp - 1.0) * dirn + comm)
    return total, trips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--split", default="2026-01-01")
    ap.add_argument("--slice", type=float, default=0.25)
    ap.add_argument("--bands", default="0.002,0.003,0.005,0.008,0.012,0.02")
    ap.add_argument("--cap", type=int, default=2)
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    pair = cfg.get("pair", "SOL_USDT")
    coin = pair.split("_")[0].lower()
    mode = cfg.get("mode", "lev")
    comm = 0.0004 if mode == "lev" else 0.0005
    holds, e = hold_intervals(a.config)
    print(f"{os.path.basename(os.path.dirname(a.config))}: {pair} {mode}, "
          f"{len(holds)} historical holds")
    if not holds:
        return
    hrs = [(x[1] - x[0]).total_seconds() / 3600 for x in holds]
    print(f"hold time: median {np.median(hrs):.1f}h, mean {np.mean(hrs):.1f}h,"
          f" max {max(hrs):.0f}h | base %/mo {e['stats']['monthly_growth_pct']:+.1f}")

    suffix = "_spot" if cfg.get("market_data") == "spot" else ""
    d1 = pd.read_parquet(os.path.join(DATA, f"{coin}{suffix}_1min.parquet"))
    d1 = d1.rename(columns=str.lower)
    d1["t"] = pd.to_datetime(d1["t"]).dt.tz_localize(None)

    split = pd.Timestamp(a.split)
    tr_h = [h for h in holds if h[1] <= split]
    ho_h = [h for h in holds if h[0] > split]
    print(f"\n{'band':>6} {'slice':>6} | {'TRAIN add%':>11} {'trips':>6} | "
          f"{'HOLDOUT add%':>13} {'trips':>6}   (add% = extra return on core "
          f"notional, fees paid)")
    for band in [float(b) for b in a.bands.split(",")]:
        t_add, t_n = grid_overlay(d1, tr_h, band, a.slice, comm, a.cap)
        h_add, h_n = grid_overlay(d1, ho_h, band, a.slice, comm, a.cap)
        print(f"{100*band:>5.2f}% {100*a.slice:>5.0f}% | "
              f"{100*t_add:>10.2f}% {t_n:>6} | {100*h_add:>12.2f}% {h_n:>6}")


if __name__ == "__main__":
    main()
