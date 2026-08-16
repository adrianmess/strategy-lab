#!/usr/bin/env python3
"""FCFS subset experiments: does dropping weak/chatty components fix the
spot combo? Tries solo-growth thresholds and reports combo growth for each."""
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


def fcfs(tabs, keep):
    allr = []
    for ci in keep:
        for et, xt, r, mae in tabs[ci]:
            allr.append((et, xt, r, ci))
    allr.sort(key=lambda x: (x[0], x[1], x[3]))
    busy, taken = -np.inf, []
    for row in allr:
        if row[0] >= busy:
            busy = row[1]
            taken.append(row)
    return taken


def mo_growth(rows):
    if not rows:
        return 0.0, 0, 0.0
    lo = min(r[0] for r in rows)
    hi = max(r[1] for r in rows)
    months = max((hi - lo) / (30.44 * _DAY), 1e-9)
    lg = sum(math.log(max(1e-9, 1.0 + max(r[2], -0.999))) for r in rows)
    # max drawdown on close-to-close equity
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in sorted(rows):
        eq *= (1.0 + max(r[2], -0.999))
        peak = max(peak, eq)
        mdd = max(mdd, 1.0 - eq / peak)
    return 100 * (math.exp(lg / months) - 1), len(rows), 100 * mdd


def solo(tabs, ci):
    return mo_growth([(et, xt, r) for et, xt, r, _ in
                      [(a, b, c, d) for a, b, c, d in tabs[ci]]])[0]


def main(run):
    comps, tabs = load(run)
    sg = [mo_growth([(et, xt, r) for et, xt, r, m in tabs[ci]])[0]
          for ci in range(len(comps))]
    order = sorted(range(len(comps)), key=lambda i: -sg[i])
    print(f"{run}: {len(comps)} comps, solo growth "
          f"{min(sg):+.1f}..{max(sg):+.1f}%/mo")
    for thr in (None, 0, 5, 7, 9, 15, 20):
        keep = (list(range(len(comps))) if thr is None
                else [i for i in range(len(comps)) if sg[i] >= thr])
        if len(keep) < 2:
            continue
        g, n, dd = mo_growth([(et, xt, r) for et, xt, r, ci in fcfs(tabs, keep)])
        lab = "ALL" if thr is None else f">={thr}%"
        print(f"  {lab:5s} -> {len(keep):2d} comps: {g:+7.1f}%/mo, "
              f"{n:4d} trades, dd {dd:.0f}%")
    for k in (2, 3, 4, 5, 6):
        keep = order[:k]
        g, n, dd = mo_growth([(et, xt, r) for et, xt, r, ci in fcfs(tabs, keep)])
        print(f"  top-{k} ({', '.join(comps[i][:28] for i in keep)}): "
              f"{g:+.1f}%/mo, {n} trades, dd {dd:.0f}%")


if __name__ == "__main__":
    main(sys.argv[1])
