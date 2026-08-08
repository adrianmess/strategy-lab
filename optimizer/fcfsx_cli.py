#!/usr/bin/env python3
"""FCFSX — the first-come-first-served combo.

The user-model router: load EXACTLY the runs you picked, all live at the
same time, each simulated on ITS OWN pair/timeframe/market candles (reusing
metax2's isolated collect subprocess). One shared position slot; whichever
component SIGNALS FIRST takes it; when the trade closes the slot frees;
repeat. No regime gating, no bucket ownership — pure time priority.

The merge itself has NO fitted parameters, so two numbers are published:
  <name>_fcfs_full  — full-history FCFS replay (the only hindsight in it is
                      that you picked the components by looking at history)
  <name>_fcfs_wf    — causal sibling: chained 42d folds where a component
                      is only in the roster if its own PAST trades (trailing
                      2 folds, >=3 closed, compounded > 0) earned the seat.
                      This is the number to believe.

Usage:
  python3 fcfsx_cli.py --name my_combo --runs run1,run2,...   (2..10 runs)

No live adapter — the panel refuses to adopt fcfsx runs.
"""
import _bootstrap as B
import argparse, fcntl, json, math, os, subprocess, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
_DAY_NS = 86_400_000_000_000
ROUTER_STRATS = ("metax", "metax2", "pairx", "fcfsx")


def fcfs_merge(tabs, t_lo=-np.inf, t_hi=np.inf, start_busy=-np.inf,
               roster=None):
    """Strict time-priority merge: all components' trades sorted by entry
    time; a trade is taken iff its entry is at/after the moment the slot
    freed. Returns (taken_rows, busy_until) — rows are (et, xt, r, mae, ci).
    Deterministic tiebreak on identical entries: earlier exit, lower index."""
    allr = []
    for ci, rows in enumerate(tabs):
        if roster is not None and ci not in roster:
            continue
        for et, xt, r, mae in rows:
            if t_lo <= et < t_hi:
                allr.append((et, xt, r, mae, ci))
    allr.sort(key=lambda x: (x[0], x[1], x[4]))
    busy, taken = start_busy, []
    for et, xt, r, mae, ci in allr:
        if et >= busy:
            busy = xt
            taken.append((et, xt, r, mae, ci))
    return taken, busy


def replay_stats(taken):
    """Equity/DD/monthly stats from FCFS rows (compounded, 1000 start)."""
    eq, peak, mdd, liq = 1000.0, 1000.0, 0.0, False
    mo, wins = {}, 0
    for et, xt, r, mae, ci in taken:
        r = max(r, -0.999)
        liq = liq or r <= -0.999
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = max(mdd, 1.0 - eq / peak)
        wins += r > 0
        m = str(np.datetime64(int(et), "ns"))[:7]
        mo[m] = mo.get(m, 1.0) * (1.0 + r)
    months = max(len(mo), 1)
    return dict(final_eq=eq, total_mult=eq / 1000.0, months=months,
                monthly_growth_pct=100 * ((max(eq, 1e-9) / 1000.0) ** (1 / months) - 1),
                liq=liq, maxdd_mtm=mdd, n=len(taken),
                tpm=len(taken) / months, sl_hits=0,
                win=wins / max(len(taken), 1)), mo


def publish(name, kind, taken, comps, mode, S, mo, note):
    eq, curve, trades = 1000.0, [], []
    for et, xt, r, mae, ci in taken:
        r = max(r, -0.999)
        eq *= (1.0 + r)
        ts = str(np.datetime64(int(et), "ns"))[:16]
        c = comps[ci]
        curve.append(dict(t=ts, eq=eq))
        trades.append(dict(entry_t=ts, exit_t=str(np.datetime64(int(xt), "ns"))[:16],
                           dir="long", entry=0.0, exit=0.0,
                           net=round(eq * r / (1 + r), 2), mae=float(mae),
                           reason=f"{c['pair'][:3]}/{c['timeframe']}·{c['strategy']}",
                           lev=1.0))
    entry = dict(name=name, stats=S,
                 curve=curve[-400:], trades=trades[-400:],
                 monthly=[dict(month=m, ret_pct=100 * (v - 1))
                          for m, v in sorted(mo.items())],
                 open_positions=[], gap_mode="skip_contaminated",
                 gap_handling=dict(threshold_min=1000, n_segments=0,
                                   skipped_gaps=[], note=note),
                 strategy="fcfsx", mode=mode, method="fcfs",
                 pair=(comps[0]["pair"] if len({c["pair"] for c in comps}) == 1
                       else "BASKET"),
                 market_data=("spot" if mode == "spot" else "perp"),
                 timeframe=("multi" if len({c["timeframe"] for c in comps}) > 1
                            else comps[0]["timeframe"]),
                 kind=kind, config=None,
                 created=time.strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(DASH, "backtests.js")
    with open(p + ".lock", "w") as lk:      # serialize with all publishers
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(p).read()
        entries = [e for e in json.JSONDecoder().raw_decode(
                       txt[txt.index("=") + 1:].lstrip())[0]
                   if e.get("name") != entry["name"]] + [entry]
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS = ")
            json.dump(entries, f, default=float)
            f.write(";")
        os.replace(tmp, p)
    print(f"published '{name}'", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=f"fcfsx_{time.strftime('%m%d_%H%M')}")
    ap.add_argument("--runs", required=True,
                    help="comma-separated run names (2..10): the EXACT "
                         "components — any pairs/timeframes, one shared mode")
    ap.add_argument("--step-days", type=int, default=42)
    ap.add_argument("--lookback-folds", type=int, default=2,
                    help="roster gate: trailing folds a component must have "
                         "been profitable over (with >=3 closed trades)")
    a = ap.parse_args()

    names = [x.strip() for x in a.runs.split(",") if x.strip()]
    if not 2 <= len(names) <= 10:
        sys.exit(f"need 2..10 runs, got {len(names)}")
    groups, mode = {}, None
    for rn in dict.fromkeys(names):
        p = os.path.join(RUNS, rn, "best_config.json")
        if not os.path.exists(p):
            sys.exit(f"run '{rn}' not found")
        b = json.load(open(p))
        strat = b.get("strategy") or (b.get("cand") or {}).get("strategy")
        if strat in ROUTER_STRATS or not b.get("cand"):
            sys.exit(f"'{rn}' is a router/empty run — pick strategy runs")
        if mode is None:
            mode = b.get("mode")
        elif b.get("mode") != mode:
            sys.exit(f"'{rn}' is {b.get('mode')} but earlier runs are "
                     f"{mode} — all components must share the mode")
        coin = (b.get("pair") or "SOL_USDT").split("_")[0].lower()
        tf = str(b.get("timeframe") or "3m")
        mkt = (b.get("market_data") or "perp").lower()
        fn = "holdout_best_config.json" if os.path.exists(
            os.path.join(RUNS, rn, "holdout_best_config.json")) \
            else "best_config.json"
        groups.setdefault((coin, tf, mkt), []).append((0.0, rn, fn, strat))
    run_dir = os.path.join(RUNS, a.name)
    os.makedirs(run_dir, exist_ok=True)
    comps, tabs = [], []
    for (coin, tf, mkt), cands in sorted(groups.items()):
        cf = os.path.join(run_dir, f"_g_{coin}_{tf}_{mkt}.json")
        of = os.path.join(run_dir, f"_t_{coin}_{tf}_{mkt}.json")
        json.dump(cands, open(cf, "w"))
        print(f"collecting {coin.upper()} {tf} {mkt} "
              f"({', '.join(d for _, d, _, _ in cands)})…", flush=True)
        rc = subprocess.run([sys.executable, "metax2_cli.py",
                             "--collect", cf, "--out", of], cwd=HERE).returncode
        if rc != 0 or not os.path.exists(of):
            print(f"  group {coin}/{tf}/{mkt} failed — skipped", flush=True)
            continue
        for d, tab in json.load(open(of)).items():
            if not tab["trades"]:
                continue
            comps.append(dict(run=d, file=tab["file"], strategy=tab["strategy"],
                              pair=f"{coin.upper()}_USDT", timeframe=tf))
            tabs.append(tab["trades"])
    if len(comps) < 2:
        sys.exit("fewer than 2 usable components survived the collect "
                 "(liquidating components are excluded there)")
    print(f"{len(comps)} components live simultaneously; "
          f"FCFS merge (one slot, first signal wins)…", flush=True)

    # ---------- full-history replay ----------
    taken, _ = fcfs_merge(tabs)
    S, mo = replay_stats(taken)
    print(f"FULL replay: {S['monthly_growth_pct']:+.1f}%/mo, "
          f"dd {100*S['maxdd_mtm']:.0f}%, {S['n']} trades"
          + (" — LIQUIDATED" if S["liq"] else ""), flush=True)
    publish(f"{a.name}_fcfs_full",
            "full-history FCFS merge (parameter-free merge — but the "
            "component CHOICE is hindsight; believe the _fcfs_wf sibling)",
            taken, comps, mode, S, mo,
            "FCFS combo: components simulated on their own datasets, one "
            "slot, first signal wins")

    # ---------- causal walk-forward sibling ----------
    step = a.step_days * _DAY_NS
    look = a.lookback_folds * step
    t_first = min(r[0][0] for r in tabs if r)
    t_last = max(max(x[1] for x in r) for r in tabs if r)
    t0 = int(np.datetime64("2025-01-01").astype("datetime64[ns]").astype("int64"))
    T = max(t0, int(t_first + 200 * _DAY_NS))
    chained, folds, busy = [], 0, -np.inf
    while T < t_last - _DAY_NS:
        roster = set()
        for ci, rows in enumerate(tabs):
            past = [(et, xt, r, mae) for et, xt, r, mae in rows
                    if T - look <= et and xt <= T]
            if len(past) >= 3:
                g = 1.0
                for _, _, r, _ in past:
                    g *= (1.0 + max(r, -0.999))
                if g > 1.0:
                    roster.add(ci)
        d = str(np.datetime64(int(T), "ns"))[:10]
        if not roster:
            print(f"  fold @{d}: no component earned the roster — flat",
                  flush=True)
        else:
            took, busy = fcfs_merge(tabs, T, T + step, start_busy=busy,
                                    roster=roster)
            print(f"  fold @{d}: roster "
                  f"{[comps[ci]['run'][:24] for ci in sorted(roster)]} "
                  f"-> {len(took)} trades", flush=True)
            chained += took
        folds += 1
        T += step
    S2, mo2 = replay_stats(chained)
    pct = S2["monthly_growth_pct"]
    verdict = "PASS" if (pct > 0 and not S2["liq"] and S2["maxdd_mtm"] <= 0.5) \
        else "FAIL"
    print(f"FCFSX VERDICT ({a.name}): {verdict} — chained OOS {pct:+.1f}%/mo, "
          f"dd {100*S2['maxdd_mtm']:.0f}%, {S2['n']} trades over {folds} folds",
          flush=True)
    publish(f"{a.name}_fcfs_wf",
            "FCFS walk-forward (CAUSAL: per-fold roster from past-only "
            "trades — this IS the honest number)",
            chained, comps, mode, S2, mo2,
            "FCFS combo walk-forward: roster re-earned every fold from "
            "past-only trades")

    json.dump(dict(strategy="fcfsx", mode=mode, method="fcfs",
                   pair=(comps[0]["pair"] if len({c["pair"] for c in comps}) == 1
                         else "BASKET"),
                   market_data=("spot" if mode == "spot" else "perp"),
                   timeframe="multi", algo="fcfs-merge",
                   cand=dict(strategy="fcfsx", components=comps,
                             live_replication="NOT YET SUPPORTED — research "
                             "artifact; panel refuses adoption"),
                   metrics=S2, holdout=S2, evaluated=folds,
                   generated=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "best_config.json"), "w"),
              indent=1, default=float)
    json.dump(dict(pool=[], evaluated=folds, seed_base=0, reservoir=[],
                   res_seen=0, runtime_s=0),
              open(os.path.join(run_dir, "pool2.json"), "w"))
    json.dump(dict(verdict=verdict, oos_pct_mo=pct, maxdd=S2["maxdd_mtm"],
                   n=S2["n"], folds=folds, step_days=a.step_days,
                   note="causal: per-fold FCFS roster from past-only trades",
                   at=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "walkforward.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
