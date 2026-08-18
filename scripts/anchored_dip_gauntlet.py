#!/usr/bin/env python3
"""The ANCHORED DIP-BUYER through the lab's real evidence standard.

The single train/holdout split said +7%/mo out-of-sample. That is one
arbitrary cut of the calendar, and this strategy has only ~46 losing events
in it — exactly the regime where one lucky split flatters a idea. So run it
the way every other candidate here is judged:

  * the FIVE canonical holdout shapes (after / before / between / outside /
    alternating 30d blocks) — a parameter set must earn its keep on data it
    never influenced, five different ways
  * a chained WALK-FORWARD: 42-day folds, each fold trading with the
    parameters chosen on PAST folds only (the honest simulation of live use)
  * a stranded-policy sweep, because that tail is where the money is:
      settle  — sell at the core's exit price (what the first test assumed)
      hold    — keep holding until price recovers to the anchor, however
                long that takes (models "it's a separate account, I can
                wait"), with the leftover marked to market at data end

Verdict language matches the rest of the lab: PASS needs positive growth in
EVERY holdout AND a positive chained walk-forward.

  python3 anchored_dip_gauntlet.py --config <core component config>
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
_DAY = pd.Timedelta(days=1)


def trades_in_window(w, E, k, comm, depth, policy, future=None):
    """Companion trades inside ONE core-hold window.
    Returns list of (exit_time, return) plus the still-open entries."""
    lo = w["low"].to_numpy()
    hi = w["high"].to_numpy()
    ts = w["t"].to_numpy()
    out, open_pos = [], []
    for i in range(len(lo)):
        if open_pos and hi[i] >= E:
            for ep in open_pos:
                out.append((ts[i], (E / ep - 1.0) - 2 * comm))
            open_pos = []
        while len(open_pos) < depth:
            nxt = E * (1 - k * (len(open_pos) + 1))
            if lo[i] <= nxt:
                open_pos.append(nxt)
            else:
                break
    if open_pos:
        if policy == "settle":
            last_t, last_p = ts[-1], float(w["close"].iloc[-1])
            for ep in open_pos:
                out.append((last_t, (last_p / ep - 1.0) - 2 * comm))
        else:                       # hold until recovery, even after the
            #                         core trade has closed
            fut = future
            rec = None
            if fut is not None and len(fut):
                hit = np.where(fut["high"].to_numpy() >= E)[0]
                if len(hit):
                    rec = fut["t"].to_numpy()[hit[0]]
            for ep in open_pos:
                if rec is not None:
                    out.append((rec, (E / ep - 1.0) - 2 * comm))
                else:               # never recovered in the data we have
                    last_t = (fut["t"].to_numpy()[-1] if fut is not None
                              and len(fut) else w["t"].to_numpy()[-1])
                    last_p = float(fut["close"].iloc[-1] if fut is not None
                                   and len(fut) else w["close"].iloc[-1])
                    out.append((last_t, (last_p / ep - 1.0) - 2 * comm))
    return out


def collect(holds, d1, k, comm, depth, policy):
    """All companion trades across every core-hold window, time ordered."""
    all_t = []
    for (s, en, E) in holds:
        w = d1[(d1["t"] >= s) & (d1["t"] <= en)]
        if len(w) < 5:
            continue
        fut = d1[d1["t"] > en] if policy == "hold" else None
        all_t += trades_in_window(w, E, k, comm, depth, policy, fut)
    all_t.sort(key=lambda x: x[0])
    return all_t


def growth(trades, depth, t0=None, t1=None):
    """%/mo of a compounding account, stake = equity/depth per slice."""
    if not trades:
        return 0.0, 0, 0.0
    eq, peak, mdd = 1.0, 1.0, 0.0
    for _, r in trades:
        eq += (eq / depth) * r
        peak = max(peak, eq)
        mdd = max(mdd, 1.0 - eq / peak)
    a = t0 or trades[0][0]
    b = t1 or trades[-1][0]
    months = max((pd.Timestamp(b) - pd.Timestamp(a)).days / 30.44, 1e-9)
    return 100 * (max(eq, 1e-9) ** (1 / months) - 1), len(trades), 100 * mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--ks", default="0.005,0.01,0.015,0.02,0.03")
    ap.add_argument("--depths", default="1,3")
    ap.add_argument("--policies", default="settle,hold")
    a = ap.parse_args()

    import backtest_cli as BT
    cfg = json.load(open(a.config))
    pair, mode = cfg.get("pair"), cfg.get("mode", "spot")
    comm = 0.0004 if mode == "lev" else 0.0005
    e = BT.run_single(a.config)
    holds = []
    for t in e["trades"]:
        if t.get("dir", "long") == "long":
            holds.append((pd.Timestamp(t["entry_t"]),
                          pd.Timestamp(t["exit_t"]), float(t["entry"])))
    for op in (e.get("open_positions") or []):
        holds.append((pd.Timestamp(op["entry_t"]),
                      pd.Timestamp(op["as_of"]), float(op["entry"])))
    coin = pair.split("_")[0].lower()
    suffix = "_spot" if cfg.get("market_data") == "spot" else ""
    d1 = pd.read_parquet(os.path.join(DATA, f"{coin}{suffix}_1min.parquet"))
    d1 = d1.rename(columns=str.lower)
    d1["t"] = pd.to_datetime(d1["t"]).dt.tz_localize(None)
    T0, T1 = d1["t"].iloc[0], d1["t"].iloc[-1]

    # the five canonical holdout shapes, as (label, predicate on exit time)
    A = pd.Timestamp("2026-02-01")
    B = pd.Timestamp("2025-06-01")
    BTW = (pd.Timestamp("2025-09-01"), pd.Timestamp("2025-12-01"))
    OUT = (pd.Timestamp("2025-06-01"), pd.Timestamp("2026-02-01"))
    HOLDOUTS = {
        "hA(after)": lambda t: t >= A,
        "hB(before)": lambda t: t < B,
        "hBtw(between)": lambda t: BTW[0] <= t < BTW[1],
        "hOut(outside)": lambda t: t < OUT[0] or t >= OUT[1],
        "hAlt30": lambda t: ((pd.Timestamp(t) - T0).days // 30) % 2 == 1,
    }

    print(f"ANCHORED DIP-BUYER gauntlet — {pair} {mode}, anchored to "
          f"{os.path.basename(os.path.dirname(a.config))}")
    print(f"{len(holds)} core-hold windows, data {T0.date()} → {T1.date()}, "
          f"fees {100*comm:.2f}%/side")
    print("\nPASS = positive in ALL FIVE holdouts AND a positive chained "
          "walk-forward (42d folds)\n")
    hdr = (f"{'policy':>7} {'k':>6} {'depth':>6} | " +
           " ".join(f"{h.split('(')[0]:>7}" for h in HOLDOUTS) +
           f" | {'WF %/mo':>8} {'WF n':>5} {'maxDD':>6}  verdict")
    print(hdr)

    for policy in a.policies.split(","):
        for k in [float(x) for x in a.ks.split(",")]:
            for depth in [int(x) for x in a.depths.split(",")]:
                tr = collect(holds, d1, k, comm, depth, policy)
                if len(tr) < 30:
                    continue
                cells, ok = [], True
                for lbl, pred in HOLDOUTS.items():
                    sub = [x for x in tr if pred(pd.Timestamp(x[0]))]
                    g = growth(sub, depth)[0] if len(sub) >= 10 else 0.0
                    cells.append(g)
                    if g <= 0:
                        ok = False
                # chained walk-forward: 42d folds, trade a fold only if the
                # trailing 2 folds were profitable (same gate the routers use)
                step = pd.Timedelta(days=42)
                t = pd.Timestamp(T0) + step * 2
                chained, eq_ok = [], True
                while t < pd.Timestamp(T1):
                    past = [x for x in tr
                            if t - 2 * step <= pd.Timestamp(x[0]) < t]
                    fold = [x for x in tr
                            if t <= pd.Timestamp(x[0]) < t + step]
                    if len(past) >= 3:
                        gpast = 1.0
                        for _, r in past:
                            gpast *= (1 + r / depth)
                        if gpast > 1.0:
                            chained += fold
                    t += step
                wf, wfn, wfdd = growth(chained, depth)
                verdict = "PASS" if (ok and wf > 0) else "fail"
                print(f"{policy:>7} {100*k:>5.1f}% {depth:>6} | " +
                      " ".join(f"{c:>+6.1f}%" for c in cells) +
                      f" | {wf:>+7.1f}% {wfn:>5} {wfdd:>5.1f}%  {verdict}")


if __name__ == "__main__":
    main()
