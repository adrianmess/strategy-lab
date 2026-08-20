#!/usr/bin/env python3
"""RESEARCH: what would an FCFS router do with N CONCURRENT slots, each
position sized 1/N of portfolio equity?

The live adapter runs ONE slot (first signal wins, everything else waits).
This re-merges the SAME component trade tables (cached in the router run's
_t_*.json) under a multi-slot rule and publishes the result as backtest
entries so the difference is visible on the Backtests page:

    <run>_fcfsN_full   full-history replay, N slots @ 1/N sizing
    <run>_fcfsN_wf     causal sibling (42d folds, trailing-past roster gate)

Model: portfolio equity compounds on CLOSED trades. A signal is taken iff a
slot is free; its stake is (equity / N), capped by free cash. On close the
portfolio books stake * r. Drawdown is mark-to-market: each open position's
MAE is applied against the portfolio at its worst point.

  python3 fcfs_multislot.py --run All_pairs_1m-3m_multi-strat_26_ --slots 2,4
"""
import argparse
import fcntl
import glob
import json
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
RUNS = os.path.join(REPO, "optimizer", "runs")
DASH = os.path.join(REPO, "dashboard")
_DAY = 86_400_000_000_000
STEP = 42 * _DAY


def load_tables(run):
    comps, tabs = [], []
    for f in sorted(glob.glob(os.path.join(RUNS, run, "_t_*.json"))):
        base = os.path.basename(f)[3:-5]          # _t_<coin>_<tf>_<mkt>.json
        coin, tf, mkt = base.split("_")
        for d, tab in json.load(open(f)).items():
            if tab["trades"]:
                comps.append(dict(run=d, pair=f"{coin.upper()}_USDT",
                                  timeframe=tf, market=mkt,
                                  strategy=tab.get("strategy")))
                tabs.append(tab["trades"])
    return comps, tabs


def simulate(tabs, slots, t_lo=-np.inf, t_hi=np.inf, roster=None, eq0=1000.0):
    """Event-driven multi-slot portfolio replay. Returns (equity curve rows,
    taken trades, stats bits). Each row: (time_ns, equity)."""
    cand = []
    for ci, rows in enumerate(tabs):
        if roster is not None and ci not in roster:
            continue
        for et, xt, r, mae in rows:
            if t_lo <= et < t_hi:
                cand.append((et, xt, r, mae, ci))
    cand.sort(key=lambda x: (x[0], x[1], x[4]))

    eq, cash = eq0, eq0
    open_pos = []          # (xt, stake, r, mae, ci, et)
    curve, taken = [], []
    peak, mdd = eq0, 0.0

    def close_due(upto):
        nonlocal eq, cash, peak, mdd
        while open_pos and min(p[0] for p in open_pos) <= upto:
            p = min(open_pos, key=lambda x: x[0])
            open_pos.remove(p)
            xt, stake, r, mae, ci, et = p
            pnl = stake * max(r, -0.999)
            eq += pnl
            cash += stake + pnl
            curve.append((xt, eq))
            peak = max(peak, eq)
            mdd = max(mdd, 1.0 - eq / peak)

    for et, xt, r, mae, ci in cand:
        close_due(et)
        if len(open_pos) >= slots:
            continue                       # all slots busy — signal missed
        stake = min(eq / slots, cash)
        if stake <= 1e-9:
            continue
        cash -= stake
        open_pos.append((xt, stake, r, mae, ci, et))
        taken.append((et, xt, r, mae, ci, stake))
        # mark-to-market trough while this position is open
        trough = eq + sum(p[1] * min(p[3], 0.0) for p in open_pos)
        mdd = max(mdd, 1.0 - trough / max(peak, 1e-9))
    close_due(np.inf)
    return curve, taken, dict(eq=eq, mdd=mdd)


def stats_from(curve, taken, res, t_first, t_last):
    months = max((t_last - t_first) / (30.44 * _DAY), 1e-9)
    eq = res["eq"]
    gm = (max(eq, 1e-9) / 1000.0) ** (1 / months) - 1
    mo = {}
    for (xt, e) in curve:
        mo[str(np.datetime64(int(xt), "ns"))[:7]] = e
    monthly, prev = [], 1000.0
    for m in sorted(mo):
        monthly.append(dict(month=m, ret_pct=100 * (mo[m] / prev - 1)))
        prev = mo[m]
    wins = sum(1 for t in taken if t[2] > 0)
    S = dict(final_eq=eq, total_mult=eq / 1000.0, months=len(mo) or 1,
             monthly_growth_pct=100 * gm, liq=False, maxdd_mtm=res["mdd"],
             n=len(taken), tpm=len(taken) / max(months, 1e-9), sl_hits=0,
             win=wins / max(len(taken), 1))
    return S, monthly


def publish(name, kind, note, S, monthly, curve, taken, comps, mode, slots):
    entry = dict(
        name=name, stats=S,
        curve=[dict(t=str(np.datetime64(int(t), "ns"))[:16], eq=e)
               for t, e in curve][-400:],
        trades=[dict(entry_t=str(np.datetime64(int(t[0]), "ns"))[:16],
                     exit_t=str(np.datetime64(int(t[1]), "ns"))[:16],
                     dir="long", entry=0.0, exit=0.0,
                     net=round(t[5] * t[2], 2), mae=float(t[3]),
                     reason=f"{comps[t[4]]['pair'][:3]}/"
                            f"{comps[t[4]]['timeframe']}m·"
                            f"{comps[t[4]]['strategy']}",
                     lev=1.0) for t in taken][-400:],
        monthly=monthly, open_positions=[], gap_mode="skip_contaminated",
        gap_handling=dict(threshold_min=1000, n_segments=0, skipped_gaps=[],
                          note=note),
        strategy="fcfsx", mode=mode, method=f"fcfs{slots}",
        pair=(comps[0]["pair"] if len({c["pair"] for c in comps}) == 1
              else "BASKET"),
        market_data=("spot" if mode == "spot" else "perp"),
        timeframe=("multi" if len({c["timeframe"] for c in comps}) > 1
                   else comps[0]["timeframe"] + "m"),
        kind=kind, config=None, created=time.strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(DASH, "backtests.js")
    with open(p + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(p).read()
        entries = [e for e in json.JSONDecoder().raw_decode(
                       txt[txt.index("=") + 1:].lstrip())[0]
                   if e.get("name") != name] + [entry]
        tmp = p + f".tmp{os.getpid()}"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS = ")
            json.dump(entries, f, default=float)
            f.write(";")
        os.replace(tmp, p)
    print(f"published '{name}'", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--slots", default="1,2,3,4")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--no-publish", action="store_true")
    a = ap.parse_args()

    comps, tabs = load_tables(a.run)
    if len(comps) < 2:
        raise SystemExit(f"no cached component tables in runs/{a.run} "
                         f"(run the FCFS combo first)")
    bc = os.path.join(RUNS, a.run, "best_config.json")
    mode = a.mode or (json.load(open(bc)).get("mode") if os.path.exists(bc)
                      else "lev") or "lev"
    t_first = min(r[0][0] for r in tabs if r)
    t_last = max(max(x[1] for x in r) for r in tabs if r)
    print(f"{a.run}: {len(comps)} components, mode={mode}, "
          f"{(t_last-t_first)/_DAY:.0f} days\n")
    print(f"{'slots':>5} {'stake':>6} | {'FULL %/mo':>10} {'total':>12} "
          f"{'dd':>5} {'n':>5} | {'WF %/mo':>9} {'wf total':>10} {'wf n':>5}")

    for slots in [int(x) for x in a.slots.split(",")]:
        curve, taken, res = simulate(tabs, slots)
        S, monthly = stats_from(curve, taken, res, t_first, t_last)
        # causal walk-forward sibling: same roster gate as fcfsx_cli
        T = max(int(np.datetime64("2025-01-01").astype("datetime64[ns]")
                    .astype("int64")), int(t_first + 200 * _DAY))
        wcurve, wtaken, weq = [], [], 1000.0
        while T < t_last - _DAY:
            roster = set()
            for ci, rows in enumerate(tabs):
                past = [x for x in rows if T - 2 * STEP <= x[0] and x[1] <= T]
                if len(past) >= 3:
                    g = 1.0
                    for x in past:
                        g *= (1.0 + max(x[2], -0.999))
                    if g > 1.0:
                        roster.add(ci)
            if roster:
                c2, t2, r2 = simulate(tabs, slots, T, T + STEP,
                                      roster=roster, eq0=weq)
                weq = r2["eq"]
                wcurve += c2
                wtaken += t2
            T += STEP
        wS, wmonthly = stats_from(
            wcurve, wtaken, dict(eq=weq, mdd=0.0),
            wcurve[0][0] if wcurve else t_first,
            wcurve[-1][0] if wcurve else t_last)
        print(f"{slots:>5} {100/slots:>5.0f}% | {S['monthly_growth_pct']:>+9.1f}% "
              f"{100*(S['total_mult']-1):>11.4g}% {100*S['maxdd_mtm']:>4.0f}% "
              f"{S['n']:>5} | {wS['monthly_growth_pct']:>+8.1f}% "
              f"{100*(wS['total_mult']-1):>9.4g}% {wS['n']:>5}")
        if not a.no_publish and slots > 1:
            publish(f"{a.run}_fcfs{slots}_full",
                    f"full-history FCFS merge, {slots} CONCURRENT slots @ "
                    f"{100//slots}% of equity each (research variant — the "
                    f"live adapter runs 1 slot)",
                    "multi-slot FCFS research variant", S, monthly, curve,
                    taken, comps, mode, slots)
            publish(f"{a.run}_fcfs{slots}_wf",
                    f"FCFS walk-forward (CAUSAL), {slots} concurrent slots @ "
                    f"{100//slots}% each — believe THIS one",
                    "multi-slot FCFS research variant", wS, wmonthly, wcurve,
                    wtaken, comps, mode, slots)


if __name__ == "__main__":
    main()
