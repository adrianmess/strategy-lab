#!/usr/bin/env python3
"""Decompose a 2-component FCFS combo: each member ALONE vs combined,
both full-history and wf-style (42d folds, trailing-2-fold roster gate) —
so every step of the combo's discount is a number, not a theory."""
import glob
import json
import math
import os
import sys

import numpy as np

OPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "optimizer")
RUNS = os.path.join(OPT, "runs")
_DAY = 86_400_000_000_000
STEP = 42 * _DAY


def load(run):
    comps, tabs = [], []
    for tf_file in sorted(glob.glob(os.path.join(RUNS, run, "_t_*.json"))):
        for d, tab in json.load(open(tf_file)).items():
            if tab["trades"]:
                comps.append(d)
                tabs.append(tab["trades"])
    return comps, tabs


def fcfs(tabs, keep, t_lo=-np.inf, t_hi=np.inf, busy=-np.inf, roster=None):
    allr = []
    for ci in keep:
        if roster is not None and ci not in roster:
            continue
        for et, xt, r, mae in tabs[ci]:
            if t_lo <= et < t_hi:
                allr.append((et, xt, r, ci))
    allr.sort(key=lambda x: (x[0], x[1], x[3]))
    taken = []
    for row in allr:
        if row[0] >= busy:
            busy = row[1]
            taken.append(row)
    return taken, busy


def stats(rows):
    if not rows:
        return 0.0, 0.0, 0
    lo = min(r[0] for r in rows)
    hi = max(r[1] for r in rows)
    months = max((hi - lo) / (30.44 * _DAY), 1e-9)
    lg = sum(math.log(max(1e-9, 1.0 + max(r[2], -0.999))) for r in rows)
    return 100 * (math.exp(lg / months) - 1), 100 * (math.exp(lg) - 1), len(rows)


def wf(tabs, keep):
    t_first = min(r[0][0] for ci in keep for r in [tabs[ci]])
    t_last = max(max(x[1] for x in tabs[ci]) for ci in keep)
    t0 = int(np.datetime64("2025-01-01").astype("datetime64[ns]").astype("int64"))
    T = max(t0, int(t_first + 200 * _DAY))
    chained, busy, flat = [], -np.inf, 0
    folds = 0
    while T < t_last - _DAY:
        roster = set()
        for ci in keep:
            past = [(et, xt, r, mae) for et, xt, r, mae in tabs[ci]
                    if T - 2 * STEP <= et and xt <= T]
            if len(past) >= 3:
                g = 1.0
                for _, _, r, _ in past:
                    g *= (1.0 + max(r, -0.999))
                if g > 1.0:
                    roster.add(ci)
        if roster:
            took, busy = fcfs(tabs, keep, T, T + STEP, busy, roster)
            chained += took
        else:
            flat += 1
        folds += 1
        T += STEP
    return chained, folds, flat


def main(run):
    comps, tabs = load(run)
    print(f"{run} — same configs the combo trades (holdout genomes):\n")
    rows = []
    for label, keep in ([(comps[ci][:34], [ci]) for ci in range(len(comps))]
                        + [("COMBINED (FCFS)", list(range(len(comps))))]):
        fg, ft, fn = stats(fcfs(tabs, keep)[0])
        ch, folds, flat = wf(tabs, keep)
        wg, wt, wn = stats(ch)
        print(f"{label:36s} full {fg:+6.1f}%/mo (total {ft:+12.4g}%, n {fn:4d})"
              f" | wf {wg:+6.1f}%/mo (total {wt:+10.4g}%, n {wn:4d},"
              f" {flat}/{folds} folds flat)")


if __name__ == "__main__":
    main(sys.argv[1])
