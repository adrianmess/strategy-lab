#!/usr/bin/env python3
"""Why is the SPOT FCFS combo worse than its best single components?
Diagnoses one fcfsx run dir: per-component solo growth vs what the combo
actually took from it, slot occupancy, and signal-overlap (contention)."""
import glob
import json
import math
import os
import sys

import numpy as np

OPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "optimizer")
RUNS = os.path.join(OPT, "runs")
_DAY = 86_400_000_000_000


def load(run):
    comps, tabs = [], []
    for tf_file in sorted(glob.glob(os.path.join(RUNS, run, "_t_*.json"))):
        for d, tab in json.load(open(tf_file)).items():
            if tab["trades"]:
                comps.append(d)
                tabs.append(tab["trades"])
    return comps, tabs


def fcfs(tabs):
    allr = []
    for ci, rows in enumerate(tabs):
        for et, xt, r, mae in rows:
            allr.append((et, xt, r, mae, ci))
    allr.sort(key=lambda x: (x[0], x[1], x[4]))
    busy, taken = -np.inf, []
    for row in allr:
        if row[0] >= busy:
            busy = row[1]
            taken.append(row)
    return taken


def mo_growth(rows):
    """monthly compounded growth % from (et, xt, r, ...) rows"""
    if not rows:
        return 0.0, 0
    lo = min(r[0] for r in rows)
    hi = max(r[1] for r in rows)
    months = max((hi - lo) / (30.44 * _DAY), 1e-9)
    lg = sum(math.log(max(1e-9, 1.0 + max(r[2], -0.999))) for r in rows)
    return 100 * (math.exp(lg / months) - 1), len(rows)


def main(run):
    comps, tabs = load(run)
    taken = fcfs(tabs)
    by_ci = {}
    for row in taken:
        by_ci.setdefault(row[4], []).append(row)
    combo_g, combo_n = mo_growth(taken)
    print(f"{run}: {len(comps)} comps | combo FULL {combo_g:+.1f}%/mo, "
          f"{combo_n} trades")
    print(f"{'component':46s} {'solo%/mo':>9s} {'solo_n':>6s} "
          f"{'taken_n':>7s} {'share':>6s} {'avg_r_solo':>10s} {'avg_r_taken':>11s}")
    for ci, d in enumerate(comps):
        sg, sn = mo_growth(tabs[ci])
        tk = by_ci.get(ci, [])
        ar_s = np.mean([t[2] for t in tabs[ci]]) if tabs[ci] else 0
        ar_t = np.mean([t[2] for t in tk]) if tk else 0
        print(f"{d[:46]:46s} {sg:+8.1f}% {sn:6d} {len(tk):7d} "
              f"{100 * len(tk) / max(combo_n, 1):5.1f}% {100 * ar_s:+9.2f}% "
              f"{100 * ar_t:+10.2f}%")

    # Contention: how often was a component's trade blocked by the slot, and
    # what was the blocked trade's return (the opportunity cost of FCFS)?
    allr = []
    for ci, rows in enumerate(tabs):
        for et, xt, r, mae in rows:
            allr.append((et, xt, r, mae, ci))
    allr.sort(key=lambda x: (x[0], x[1], x[4]))
    busy = -np.inf
    blocked = []
    for et, xt, r, mae, ci in allr:
        if et >= busy:
            busy = xt
        else:
            blocked.append(r)
    n_all = len(allr)
    print(f"\ncontention: {len(blocked)}/{n_all} signals blocked "
          f"({100 * len(blocked) / max(n_all, 1):.0f}%)")
    if blocked:
        print(f"blocked trades avg r {100 * np.mean(blocked):+.2f}% vs taken "
              f"avg r {100 * np.mean([t[2] for t in taken]):+.2f}%")
    # time-in-market
    span = max(x[1] for x in allr) - min(x[0] for x in allr)
    occ = sum(t[1] - t[0] for t in taken)
    print(f"slot occupancy: {100 * occ / span:.0f}% of the {span / _DAY:.0f}-day span")
    # overlap between components (signal correlation proxy)
    starts = sorted(x[0] for x in allr)
    dup = sum(1 for i in range(1, n_all)
              if starts[i] - starts[i - 1] < 6 * 3_600_000_000_000)
    print(f"signal clustering: {100 * dup / max(n_all - 1, 1):.0f}% of signals "
          f"arrive within 6h of the previous one")


if __name__ == "__main__":
    main(sys.argv[1])
