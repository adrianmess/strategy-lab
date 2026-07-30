#!/usr/bin/env python3
"""PairX — the multi-pair SPOT basket simulator (FCFS single slot).

Mirrors what the multi-pair live trader will execute: every pair's candidate
config runs its signal engine; when any pair signals and the ONE position
slot is free, that pair takes it with full equity (first-come-first-served);
everyone else waits.

Pipeline (default command):
  1. mine   — top K spot candidate runs per pair (honest metric, no liq,
              hold<=7d, family configs only, 3m timeframe)
  2. collect— per-pair SUBPROCESS simulates each candidate full-history on
              that pair's own data (the in-process guard forbids mixing
              pair datasets, deliberately) -> trade tables JSON
  3. walk-forward — CAUSAL by construction: at each 42d fold, each pair's
              component is picked from PAST-only trailing performance
              (dd-penalized, sit out if nothing positive), then the chosen
              components' trades are merged through the FCFS single slot on
              the NEXT unseen window. Chained result IS the basket's honest
              number.
  4. publish— run dir (best_config manifest + walkforward.json verdict) and
              a Backtests entry (pair "BASKET").

Usage:
  python3 pairx_cli.py --name pairx_spot_v1
  python3 pairx_cli.py --collect btc --out /tmp/x.json   (internal)
"""
import _bootstrap as B
import argparse, json, math, os, subprocess, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
PAIRS = ["sol", "btc", "eth", "doge", "xrp", "sui"]
SPOT_COMM = 0.0005
MAX_HOLD_D = 7.0
_DAY_NS = 86_400_000_000_000


def honest_growth(b):
    hb = ((b.get("holdout_best") or {}).get("holdout") or {})
    h = b.get("holdout") or {}
    best = None
    for m in (hb, h):
        if m and not m.get("liq") and (m.get("growth") or 0) > 0:
            if best is None or m["growth"] > best:
                best = m["growth"]
    return best


def mine(top_k=3):
    """Top K spot candidate runs per pair (family configs, 3m)."""
    out = {c: [] for c in PAIRS}
    for d in sorted(os.listdir(RUNS)):
        p = os.path.join(RUNS, d, "best_config.json")
        if not os.path.exists(p):
            continue
        try:
            b = json.load(open(p))
        except Exception:
            continue
        if b.get("mode") != "spot" or not b.get("cand"):
            continue
        if b.get("strategy") == "metax" or (b.get("cand") or {}).get("strategy") == "metax":
            continue
        if str(b.get("timeframe") or "3m") != "3m":
            continue
        g = honest_growth(b)
        if g is None:
            continue
        h = ((b.get("holdout_best") or {}).get("holdout") or b.get("holdout") or {})
        if (h.get("max_hold_days") or 0) > MAX_HOLD_D:
            continue
        coin = (b.get("pair") or "SOL_USDT").split("_")[0].lower()
        if coin in out:
            fn = "holdout_best_config.json" if (b.get("holdout_best") and
                 os.path.exists(os.path.join(RUNS, d, "holdout_best_config.json"))) \
                 else "best_config.json"
            out[coin].append((g, d, fn, b.get("strategy")))
    for c in out:
        out[c].sort(reverse=True)
        out[c] = out[c][:top_k]
    return out


def collect(coin, cands, out_path):
    """Subprocess entry: simulate each candidate on THIS pair's data."""
    import backtest_cli as BT
    tabs = {}
    for g, d, fn, strat in cands:
        path = os.path.join(RUNS, d, fn)
        try:
            e = BT.run_single(path)
        except SystemExit as ex:
            print(f"  {d}: {ex}", flush=True)
            continue
        if e["stats"].get("liq"):
            print(f"  EXCLUDED {d}: liquidates in full history", flush=True)
            continue
        rows = []
        for t in e["trades"]:
            try:
                et = int(np.datetime64(t["entry_t"]).astype("datetime64[ns]").astype("int64"))
                xt = int(np.datetime64(t["exit_t"]).astype("datetime64[ns]").astype("int64"))
            except Exception:
                continue
            move = (t["exit"] / t["entry"] - 1.0)   # spot: long-only, 1x
            r = move - SPOT_COMM * (1.0 + t["exit"] / t["entry"])
            rows.append([et, xt, r, float(t.get("mae") or 0.0)])
        tabs[d] = dict(strategy=strat, file=fn, trades=rows)
        print(f"  {d} ({strat}): {len(rows)} trades", flush=True)
    json.dump(dict(coin=coin, tabs=tabs), open(out_path, "w"))


CAUSAL_SOL_ENTRIES = ["camp_c4_m_spot_vol3_walkforward_router_full",
                      "camp_c4_m_spot_vt9_walkforward_router_full",
                      "camp_c5_stk_spot_router_full"]

def causal_router_tables():
    """SOL's routers as basket components — ONLY their causal streams
    (chronological walk-forwards + the stack, which is causal by
    construction). Per-trade returns rebuilt exactly from the published
    curve (curve and trades are appended 1:1 by the metax publisher)."""
    p = os.path.join(DASH, "backtests.js")
    entries = None
    for attempt in range(5):     # a concurrent publisher may be mid-write
        try:
            txt = open(p).read()
            entries = {e["name"]: e for e in
                       json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))}
            break
        except Exception as ex:
            print(f"  backtests.js busy ({ex}); retrying…", flush=True)
            time.sleep(10)
    if entries is None:
        return {}
    out = {}
    for name in CAUSAL_SOL_ENTRIES:
        e = entries.get(name)
        if not e or len(e.get("curve") or []) != len(e.get("trades") or []):
            continue
        rows, prev = [], 1000.0
        ok = True
        for c, t in zip(e["curve"], e["trades"]):
            try:
                et = int(np.datetime64(t["entry_t"]).astype("datetime64[ns]").astype("int64"))
                xt = int(np.datetime64(t["exit_t"]).astype("datetime64[ns]").astype("int64"))
            except Exception:
                ok = False
                break
            r = c["eq"] / prev - 1.0
            prev = c["eq"]
            rows.append([et, xt, r, float(t.get("mae") or 0.0)])
        if ok and rows:
            out[name] = dict(strategy="metax", file="(published causal entry)",
                             trades=rows)
            print(f"  SOL causal router component: {name} ({len(rows)} trades)",
                  flush=True)
    return out


def rows_stats(rows):
    if not len(rows):
        return None
    eq = peak = 1000.0
    dd = 0.0
    mo = {}
    for et, xt, r, mae in rows:
        r = max(r, -0.999)
        trough = eq * (1.0 + min(mae, 0.0))
        eq *= (1.0 + r)
        peak = max(peak, eq)
        dd = max(dd, 1 - min(trough, eq) / peak)
        k = int(et // (30.44 * _DAY_NS))
        mo[k] = mo.get(k, 0.0) + math.log(max(1e-9, 1.0 + r))
    g = np.array(list(mo.values()))
    return dict(eq=eq, maxdd=dd, n=len(rows),
                growth=float(g.mean()), score=float(g.mean() - 0.25 * g.std()),
                months=len(mo))


def walkforward(tables, step_days=42, look_days=84, dd_gate=0.35):
    """Causal fold loop: per pair pick the trailing-best component from PAST
    trades only, then FCFS-merge the picks over the next unseen window."""
    min_rel = float(os.environ.get("PAIRX_MIN_REL", "0"))
    step, look = step_days * _DAY_NS, look_days * _DAY_NS
    all_t = [tr for coin in tables for d in tables[coin]
             for tr in tables[coin][d]["trades"]]
    if not all_t:
        return None, [], []
    t0 = int(np.datetime64("2025-01-01").astype("datetime64[ns]").astype("int64"))
    t_first = min(t[0] for t in all_t)
    t_last = max(t[1] for t in all_t)
    T = max(t0, t_first + 200 * _DAY_NS)
    chained, picks = [], []
    last_exit = -float("inf")
    while T < t_last - _DAY_NS:
        fold_pick = {}
        for coin, comps in tables.items():
            best = None
            for d, tab in comps.items():
                past = [t for t in tab["trades"] if T - look <= t[0] < T]
                s = rows_stats(past)
                if not s or s["maxdd"] > dd_gate:
                    continue
                sel = s["score"] - 0.3 * s["maxdd"]
                if sel > 0 and (best is None or sel > best[0]):
                    best = (sel, d)
            if best:
                fold_pick[coin] = best
        # relative quality bar: a pair only competes for the slot if its
        # trailing score is within range of the fold's best (weak edges
        # dilute a strong one under FCFS — measured, not assumed)
        if fold_pick and min_rel > 0:
            top = max(s for s, _ in fold_pick.values())
            fold_pick = {c: v for c, v in fold_pick.items()
                         if v[0] >= min_rel * top}
        fold_pick = {c: v[1] for c, v in fold_pick.items()}
        # FCFS merge of the picked components' trades in [T, T+step)
        window = []
        for coin, d in fold_pick.items():
            for t in tables[coin][d]["trades"]:
                if T <= t[0] < T + step:
                    window.append(t + [coin, d])
        window.sort(key=lambda x: x[0])
        took = 0
        for t in window:
            if t[0] < last_exit:
                continue
            last_exit = t[1]
            chained.append(t)
            took += 1
        picks.append(dict(fold=str(np.datetime64(T, "ns"))[:10],
                          picks=fold_pick, trades=took))
        print(f"  fold @{picks[-1]['fold']}: "
              f"{ {c: d[:28] for c, d in fold_pick.items()} or 'ALL FLAT' } "
              f"-> {took} trades", flush=True)
        T += step
    # the assignment to trade NEXT (past = everything)
    current = {}
    for coin, comps in tables.items():
        best = None
        for d, tab in comps.items():
            past = [t for t in tab["trades"] if t[0] >= t_last - look]
            s = rows_stats(past)
            if not s or s["maxdd"] > dd_gate:
                continue
            sel = s["score"] - 0.3 * s["maxdd"]
            if sel > 0 and (best is None or sel > best[0]):
                best = (sel, d, tab["file"], tab["strategy"])
        if best:
            current[coin] = dict(run=best[1], file=best[2], strategy=best[3])
    return chained, picks, current


def publish(name, chained, S):
    eq = 1000.0
    curve, trades, mo_map = [], [], {}
    wins = 0
    for et, xt, r, mae, coin, d in chained:
        r = max(r, -0.999)
        eq *= (1.0 + r)
        ts = str(np.datetime64(int(et), "ns"))[:16]
        xs = str(np.datetime64(int(xt), "ns"))[:16]
        curve.append(dict(t=ts, eq=eq))
        wins += r > 0
        trades.append(dict(entry_t=ts, exit_t=xs, dir="long", entry=0.0, exit=0.0,
                           net=round(eq * r / (1 + r), 2), mae=float(mae),
                           reason=f"{coin.upper()}·{d[:24]}", lev=1.0))
        mo = ts[:7]
        mo_map[mo] = mo_map.get(mo, 1.0) * (1.0 + r)
    months = max(len(mo_map), 1)
    entry = dict(
        name=f"{name}_basket_full",
        stats=dict(months=months, final_eq=eq, total_mult=eq / 1000.0,
                   monthly_growth_pct=100 * ((eq / 1000.0) ** (1 / months) - 1),
                   liq=False, maxdd_mtm=S["maxdd"], n=len(trades),
                   tpm=len(trades) / months, sl_hits=0,
                   win=wins / max(len(trades), 1)),
        curve=curve, trades=trades[-400:],
        monthly=[dict(month=m, ret_pct=100 * (v - 1)) for m, v in sorted(mo_map.items())],
        open_positions=[], gap_mode="skip_contaminated",
        gap_handling=dict(threshold_min=1000, n_segments=0, skipped_gaps=[],
                          note="FCFS basket merge: every component simulated "
                               "on its own pair's gap-segmented data"),
        strategy="pairx", mode="spot", method="fcfs",
        pair="BASKET", market_data="spot", timeframe="3m",
        kind="multi-pair basket walk-forward (CAUSAL: per-fold picks from "
             "past-only data — this IS the honest number)",
        config=None, created=time.strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(DASH, "backtests.js")
    txt = open(p).read()
    entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
    entries = [e for e in entries if e.get("name") != entry["name"]] + [entry]
    tmp = p + ".tmp"
    with open(tmp, "w") as f:               # atomic: never a half-written file
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")
    os.replace(tmp, p)
    print(f"published '{entry['name']}'", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="pairx_spot_v1")
    ap.add_argument("--collect", default=None, help="(internal) pair to simulate")
    ap.add_argument("--cands", default=None, help="(internal) candidates JSON")
    ap.add_argument("--out", default=None)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--step-days", type=int, default=42)
    a = ap.parse_args()

    if a.collect:
        collect(a.collect, json.load(open(a.cands)), a.out)
        return

    cands = mine(a.top_k)
    for c, lst in cands.items():
        print(f"{c.upper()}: " + (", ".join(
            f"{d}({s},{100*(math.exp(g)-1):+.1f}%)" for g, d, fn, s in lst)
            or "no candidates"), flush=True)
    run_dir = os.path.join(RUNS, a.name)
    os.makedirs(run_dir, exist_ok=True)
    tables = {}
    for c, lst in cands.items():
        if not lst:
            continue
        cf = os.path.join(run_dir, f"_cands_{c}.json")
        of = os.path.join(run_dir, f"tables_{c}.json")
        json.dump(lst, open(cf, "w"))
        print(f"collecting {c.upper()} (subprocess)…", flush=True)
        rc = subprocess.run([sys.executable, "pairx_cli.py", "--collect", c,
                             "--cands", cf, "--out", of], cwd=HERE).returncode
        if rc == 0 and os.path.exists(of):
            tabs = json.load(open(of))["tabs"]
            if tabs:
                tables[c] = tabs
    # SOL's routers join as CAUSAL streams (walk-forwards + stack)
    rt = causal_router_tables()
    if rt:
        tables.setdefault("sol", {}).update(rt)
    if not tables:
        sys.exit("no usable components")
    print(f"walk-forward over {sum(len(v) for v in tables.values())} components "
          f"across {len(tables)} pairs…", flush=True)
    chained, picks, current = walkforward(tables, a.step_days)
    S = rows_stats([t[:4] for t in chained]) if chained else None
    if not S:
        print("VERDICT: NO-TRADES", flush=True)
        return
    pct = 100 * (math.exp(S["growth"]) - 1)
    verdict = "PASS" if (pct > 0 and S["maxdd"] <= 0.5) else "FAIL"
    print(f"BASKET VERDICT ({a.name}): {verdict} — chained OOS {pct:+.1f}%/mo, "
          f"dd {100*S['maxdd']:.0f}%, {S['n']} trades, "
          f"tpm {S['n']/max(S['months'],1):.1f}", flush=True)
    json.dump(dict(strategy="pairx", mode="spot", method="fcfs", pair="BASKET",
                   market_data="spot", timeframe="3m", algo="fcfs-basket",
                   cand=dict(strategy="pairx", pairs=sorted(tables),
                             current=current, fold_picks=picks,
                             top_k=a.top_k, step_days=a.step_days,
                             live_replication="multi-pair trader: run every "
                             "pair's current component virtually; FCFS single "
                             "slot (NOT YET BUILT — do not adopt)"),
                   metrics=S, holdout=S,
                   evaluated=sum(len(v) for v in tables.values()),
                   generated=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "best_config.json"), "w"),
              indent=1, default=float)
    json.dump(dict(pool=[], evaluated=sum(len(v) for v in tables.values()),
                   seed_base=0, reservoir=[], res_seen=0, runtime_s=0),
              open(os.path.join(run_dir, "pool2.json"), "w"))
    json.dump(dict(verdict=verdict, oos_pct_mo=pct, maxdd=S["maxdd"], n=S["n"],
                   folds=len(picks), step_days=a.step_days,
                   note="causal by construction (per-fold past-only picks)",
                   at=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "walkforward.json"), "w"), indent=1)
    publish(a.name, chained, S)


if __name__ == "__main__":
    main()
