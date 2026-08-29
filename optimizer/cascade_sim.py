#!/usr/bin/env python3
"""CASCADE / multi-slot validation sim (research only — nothing live).

Question (Adrian, 2026-08-28): with the liquidity guardrail capping each
position at frac x depth(bps) of its pair, should leftover capital cascade
into the NEXT signal (2nd, 3rd pair...) instead of sitting idle?

Model, event-driven over the routers' engine-exact trade tables:
  BASELINE  one slot (live behavior): the earliest fresh signal takes the
            slot; margin = min(equity, cap_pair/lev); the remainder idles;
            all other signals are skipped while the slot is busy.
  CASCADE   capital pool: EVERY fresh signal opens while free margin
            remains, allocation = min(free, headroom_pair/lev), where
            headroom = frac x depth5bps - notional already in flight on that
            pair. Same trades, same returns — only allocation differs.
P&L per trade = allocated_margin x r (tables' margin-basis net return).
Depth caps are today's measured 5bps worst-side books (static — a stated
simplification). Run at several STARTING equities: caps barely bind at
$1k, so the interesting question is what happens as the account grows.

Usage: python3 cascade_sim.py [workers=12]
"""
import glob
import json
import os
import sys
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = {
    "SPOT (MEX2 Spot)": "All_pairs_SPOT_1m-3m_4Dmax_multi-strat",
    "LEV (MEX Lev 1)": "All_pairs_LEV_1m-3m_multi-strat",
}
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
DEPTH5 = dict(btc=7.9e6, eth=2.4e6, sol=3.8e6, xrp=6.1e5,
              doge=3.0e5, sui=1.9e4, hype=8.3e4)
FRAC = 0.25
MIN_ALLOC = 20.0
NS_MIN = 60_000_000_000


def load_trades(run_dir, is_lev):
    """[(et, xt, r, pair, lev)] across all components."""
    import pandas as pd
    trades = []
    for f in sorted(glob.glob(os.path.join(HERE, "runs", run_dir,
                                           "_t_*.json"))):
        pair = os.path.basename(f).split("_")[2]
        ts = px = None
        if is_lev:
            df = pd.read_parquet(os.path.join(DATA, f"{pair}_1min.parquet"))
            tc = [c for c in df.columns
                  if c.lower() in ("t", "time", "ts", "datetime", "date")][0]
            df[tc] = pd.to_datetime(df[tc])
            s = df.set_index(tc)["close"]
            if s.index.tz is not None:
                s.index = s.index.tz_localize(None)
            ts, px = s.index.asi8, s.values.astype(np.float64)
        for run, tab in json.load(open(f)).items():
            tr = tab.get("trades") or []
            if not tr:
                continue
            lev = 1.0
            if is_lev:
                et = np.array([x[0] for x in tr], dtype=np.int64)
                xt = np.array([x[1] for x in tr], dtype=np.int64)
                r = np.array([x[2] for x in tr])
                ei = np.clip(np.searchsorted(ts, et, "right") - 1, 0, None)
                xi = np.clip(np.searchsorted(ts, xt, "right") - 1, 0, None)
                pm = px[xi] / np.maximum(px[ei], 1e-12) - 1.0
                good = np.abs(pm) > 5e-4
                if good.sum() >= 3:
                    lev = float(np.clip(round(float(np.median(
                        np.abs(r[good]) / np.abs(pm[good]))), 1), 1, 100))
            for x in tr:
                et, xt = int(x[0]), int(x[1])
                if xt <= et:            # zero/negative-duration artifacts
                    xt = et + NS_MIN    # would deadlock the event model
                trades.append((et, xt, float(x[2]), pair, lev))
    trades.sort()
    return trades


def simulate(args):
    label, run_dir, is_lev, e0, cascade = args
    trades = load_trades(run_dir, is_lev)
    # event stream: (time, kind, idx)  kind 0=exit first at same ts, 1=entry
    events = []
    for i, (et, xt, r, pair, lev) in enumerate(trades):
        events.append((et, 1, i))
        events.append((xt, 0, i))
    events.sort()
    free = e0
    open_pos = {}                  # i -> (margin, pair, notional)
    pair_notional = {}
    n_open = n_skip_slot = n_skip_funds = n_capped = 0
    conc_sum = conc_n = 0
    eq_peak, mdd = e0, 0.0
    for t, kind, i in events:
        et, xt, r, pair, lev = trades[i]
        if kind == 0:
            if i not in open_pos:
                continue
            m, p, nt = open_pos.pop(i)
            pair_notional[p] = pair_notional.get(p, 0.0) - nt
            free += m * (1.0 + max(r, -1.0))
            eq = free + sum(v[0] for v in open_pos.values())
            eq_peak = max(eq_peak, eq)
            mdd = max(mdd, 1 - eq / eq_peak)
            continue
        # entry
        if not cascade and open_pos:
            n_skip_slot += 1
            continue
        cap_n = FRAC * DEPTH5.get(pair, 1e9)
        head_n = cap_n - pair_notional.get(pair, 0.0)
        alloc = min(free, max(0.0, head_n) / lev)
        if alloc < MIN_ALLOC:
            n_skip_funds += 1
            continue
        want = free
        if alloc < want - 0.01:
            n_capped += 1
        nt = alloc * lev
        open_pos[i] = (alloc, pair, nt)
        pair_notional[pair] = pair_notional.get(pair, 0.0) + nt
        free -= alloc
        n_open += 1
        conc_sum += len(open_pos)
        conc_n += 1
    eq = free + sum(v[0] for v in open_pos.values())
    t0, t1 = trades[0][0], trades[-1][1]
    months = max((t1 - t0) / (30.44 * 86400 * 1e9), 0.1)
    return dict(label=label, e0=e0, cascade=cascade, final=eq,
                mult=eq / e0,
                monthly=100 * ((eq / e0) ** (1 / months) - 1),
                dd=100 * mdd, n_open=n_open, n_skip_slot=n_skip_slot,
                n_skip_funds=n_skip_funds, n_capped=n_capped,
                avg_conc=(conc_sum / conc_n if conc_n else 0),
                months=months)


def main():
    nproc = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    jobs = []
    for label, run_dir in RUNS.items():
        is_lev = "LEV" in label
        starts = ([1256, 10e3, 50e3, 200e3] if is_lev
                  else [1116, 10e3, 50e3])
        for e0 in starts:
            for cascade in (False, True):
                jobs.append((label, run_dir, is_lev, float(e0), cascade))
    with Pool(nproc) as pool:
        res = pool.map(simulate, jobs)
    cur = None
    for r in sorted(res, key=lambda r: (r["label"], r["e0"], r["cascade"])):
        if (r["label"], r["e0"]) != cur:
            cur = (r["label"], r["e0"])
            print(f"\n{r['label']}  start ${r['e0']:,.0f} "
                  f"({r['months']:.1f} months of trades)")
        tag = "CASCADE " if r["cascade"] else "baseline"
        print(f"  {tag}: {r['monthly']:+8.1f}%/mo  x{r['mult']:.3g}  "
              f"dd {r['dd']:.0f}%  | {r['n_open']} opened, "
              f"{r['n_skip_slot']} slot-skipped, {r['n_capped']} capped, "
              f"avg {r['avg_conc']:.1f} concurrent")


if __name__ == "__main__":
    main()
