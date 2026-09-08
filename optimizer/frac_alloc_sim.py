#!/usr/bin/env python3
"""Fractional-allocation sim for the LIVE SPOT router (research only).

Question (Adrian, 2026-09-07): instead of the one-slot all-in router, what
if every trade only gets a FIXED FRACTION of capital so several positions
can be open at once?
  50% per trade -> max 2 concurrent
  33% per trade -> max 3
  25% per trade -> max 4

Model — event-driven over the router's engine-exact trade tables (same
machinery as cascade_sim.py, fresh-signal semantics, one position per
symbol, exit-before-entry at equal timestamps):
  - allocation per entry = frac x TOTAL equity (free + committed),
    capped by free capital and by the liquidity guardrail
    (25% x 5bps depth per pair, today's measured books);
  - baseline = frac 1.0, max 1 slot (the live behavior);
  - P&L per trade = allocation x r (the tables' net, fees included);
  - drawdown is REALIZED-only (no intra-trade excursions).

Usage: python3 frac_alloc_sim.py [workers=12]
"""
import sys
from multiprocessing import Pool

from cascade_sim import load_trades, DEPTH5, FRAC, NS_MIN  # noqa: F401

RUN_DIR = "All_pairs_SPOT_1m-3m_4Dmax_multi-strat"
VARIANTS = [(1.00, 1, "baseline (all-in, 1 slot)"),
            (0.50, 2, "50% per trade, max 2"),
            (1 / 3, 3, "33% per trade, max 3"),
            (0.25, 4, "25% per trade, max 4")]
STARTS = [842.0, 5e3, 20e3]        # today's MEX2 Spot equity + growth cases
MIN_ALLOC = 5.0                    # spot minimum order is ~$1; stay clear


def simulate(args):
    e0, frac, max_slots, label = args
    trades = load_trades(RUN_DIR, is_lev=False)
    events = []
    for i, (et, xt, r, pair, lev) in enumerate(trades):
        events.append((et, 1, i))
        events.append((xt, 0, i))
    events.sort()
    free = e0
    open_pos = {}                  # i -> (alloc, pair)
    pair_notional = {}
    n_open = n_skip_full = n_skip_sym = n_skip_funds = n_capped = 0
    conc_sum = conc_n = 0
    eq_peak, mdd = e0, 0.0
    for t, kind, i in events:
        et, xt, r, pair, lev = trades[i]
        if kind == 0:
            if i not in open_pos:
                continue
            m, p = open_pos.pop(i)
            pair_notional[p] = pair_notional.get(p, 0.0) - m
            free += m * (1.0 + max(r, -1.0))
            eq = free + sum(v[0] for v in open_pos.values())
            eq_peak = max(eq_peak, eq)
            mdd = max(mdd, 1 - eq / eq_peak)
            continue
        # entry
        if len(open_pos) >= max_slots:
            n_skip_full += 1
            continue
        if any(p == pair for _, p in open_pos.values()):
            n_skip_sym += 1
            continue
        equity = free + sum(v[0] for v in open_pos.values())
        alloc = min(free, frac * equity)
        head = FRAC * DEPTH5.get(pair, 1e9) - pair_notional.get(pair, 0.0)
        if alloc > max(0.0, head):
            alloc = max(0.0, head)
            n_capped += 1
        if alloc < MIN_ALLOC:
            n_skip_funds += 1
            continue
        open_pos[i] = (alloc, pair)
        pair_notional[pair] = pair_notional.get(pair, 0.0) + alloc
        free -= alloc
        n_open += 1
        conc_sum += len(open_pos)
        conc_n += 1
    eq = free + sum(v[0] for v in open_pos.values())
    months = max((trades[-1][1] - trades[0][0]) / (30.44 * 86400 * 1e9), 0.1)
    return dict(e0=e0, label=label, final=eq, mult=eq / e0,
                monthly=100 * ((eq / e0) ** (1 / months) - 1),
                dd=100 * mdd, n_open=n_open, skip_full=n_skip_full,
                skip_sym=n_skip_sym, skip_funds=n_skip_funds,
                capped=n_capped,
                avg_conc=(conc_sum / conc_n if conc_n else 0),
                months=months)


def main():
    nproc = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    jobs = [(e0, f, mx, lb) for e0 in STARTS for f, mx, lb in VARIANTS]
    with Pool(nproc) as pool:
        res = pool.map(simulate, jobs)
    cur = None
    for r in res:
        if r["e0"] != cur:
            cur = r["e0"]
            print(f"\nSPOT router  start ${r['e0']:,.0f}  "
                  f"({r['months']:.1f} months of trades)")
        print(f"  {r['label']:26s}: {r['monthly']:+7.1f}%/mo  "
              f"x{r['mult']:9.3g}  dd {r['dd']:4.1f}%  | "
              f"{r['n_open']} opened, {r['skip_full']} slot-full, "
              f"{r['skip_sym']} same-pair, {r['skip_funds']} no-funds, "
              f"{r['capped']} depth-capped, avg {r['avg_conc']:.2f} conc")


if __name__ == "__main__":
    main()
