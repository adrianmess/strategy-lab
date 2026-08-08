#!/usr/bin/env python3
"""MetaX2 — the CROSS-TIMEFRAME (and cross-pair) regime router.

Where MetaX pins one dataset, MetaX2 routes TRADE STREAMS: every component
is simulated in an isolated subprocess on ITS OWN pair/timeframe/market
candles (a 1m scalper stays a 1m scalper), and the router only decides who
owns which volatility bucket. Buckets come from the REFERENCE pair's 3m
vol terciles (wall-clock — timestamps from any chart map cleanly onto them).
Single position slot, first-come inside a bucket, exactly like MetaX.

Honesty: identical to the MetaX walk-forward — at each 42d cutoff the
assignment is re-searched from PAST trades only (dd-gated, risk-averse
score), traded on the next unseen window, chained. The chained curve is the
published number; there is no train-flavored publish at all.

Usage:
  python3 metax2_cli.py --name metax2_spot_sol                 # SOL x {1m,3m,5m}
  python3 metax2_cli.py --name metax2_spot_all --pairs all     # + every pair
  python3 metax2_cli.py --collect g.json --out o.json          # (internal)

No live adapter yet — the panel refuses to adopt metax2 runs.
"""
import _bootstrap as B
import argparse, fcntl, json, math, os, subprocess, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
SPOT_COMM = 0.0005
MAX_HOLD_D = 7.0
_DAY_NS = 86_400_000_000_000
ALL_PAIRS = ["sol", "btc", "eth", "doge", "xrp", "sui"]


def honest_growth(b):
    hb = ((b.get("holdout_best") or {}).get("holdout") or {})
    h = b.get("holdout") or {}
    best = None
    for m in (hb, h):
        if m and not m.get("liq") and (m.get("growth") or 0) > 0:
            if best is None or m["growth"] > best:
                best = m["growth"]
    return best


def mine(pairs, mode="spot", per_group=2):
    """Top surviving family runs grouped by (pair, timeframe)."""
    groups = {}
    for d in sorted(os.listdir(RUNS)):
        p = os.path.join(RUNS, d, "best_config.json")
        if not os.path.exists(p):
            continue
        try:
            b = json.load(open(p))
        except Exception:
            continue
        strat = b.get("strategy") or (b.get("cand") or {}).get("strategy")
        if b.get("mode") != mode or not b.get("cand") \
                or strat in ("metax", "metax2", "pairx", "fcfsx"):
            continue
        coin = (b.get("pair") or "SOL_USDT").split("_")[0].lower()
        if coin not in pairs:
            continue
        h = ((b.get("holdout_best") or {}).get("holdout") or b.get("holdout") or {})
        if (h.get("max_hold_days") or 0) > MAX_HOLD_D:
            continue
        g = honest_growth(b)
        if g is None:
            continue
        tf = str(b.get("timeframe") or "3m")
        fn = "holdout_best_config.json" if (b.get("holdout_best") and os.path.exists(
            os.path.join(RUNS, d, "holdout_best_config.json"))) else "best_config.json"
        groups.setdefault((coin, tf), []).append((g, d, fn, strat))
    for k in groups:
        groups[k].sort(reverse=True)
        groups[k] = groups[k][:per_group]
    return groups


def collect(cands_path, out_path):
    """Subprocess: simulate one (pair, tf, market) group on its own dataset.
    run_single self-pins the dataset from the first config; the group is
    homogeneous so no mixing can occur. Mode-aware: lev components keep
    their per-trade leverage and futures fees."""
    import backtest_cli as BT
    cands = json.load(open(cands_path))
    tabs = {}
    for g, d, fn, strat in cands:
        cfg_path = os.path.join(RUNS, d, fn)
        mode = (json.load(open(cfg_path)).get("mode") or "spot")
        comm = 0.0004 if mode == "lev" else SPOT_COMM
        try:
            e = BT.run_single(cfg_path)
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
            lev = float(t.get("lev") or 1.0)
            dr = 1.0 if t.get("dir", "long") == "long" else -1.0
            move = dr * (t["exit"] / t["entry"] - 1.0)
            r = move * lev - comm * lev * (1.0 + t["exit"] / t["entry"])
            rows.append([et, xt, r,
                         float(t.get("mae") or 0.0) * lev])
        tabs[d] = dict(strategy=strat, file=fn, trades=rows)
        print(f"  {d} ({strat}, {mode}): {len(rows)} trades", flush=True)
    json.dump(tabs, open(out_path, "w"))


def to_table(rows, times, bucket):
    """metax-format 7-col table: entry, exit, ret, mae, liq, bucket, hold."""
    out = []
    for et, xt, r, mae in rows:
        i = int(np.searchsorted(times, et))
        if i >= len(times):
            i = len(times) - 1
        out.append((et, xt, r, mae, 0.0, bucket[i],
                    (xt - et) / _DAY_NS))
    return np.array(out, dtype=np.float64) if out else np.zeros((0, 7))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="metax2_spot_sol")
    ap.add_argument("--runs", default=None,
                    help="comma-separated run names: build the router from "
                         "EXACTLY these components — pairs and timeframes may "
                         "differ (each simulates on its own candles); all "
                         "must share the same MODE")
    ap.add_argument("--extend", default=None, metavar="ROUTER",
                    help="existing metax2 run: reuse its components as the "
                         "base; combine with --add")
    ap.add_argument("--add", default=None, metavar="RUN,RUN",
                    help="runs to ADD (with --extend)")
    ap.add_argument("--pairs", default="sol",
                    help="'sol', 'all', or comma list (reference pair for the "
                         "vol buckets is always SOL 3m)")
    ap.add_argument("--collect", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--per-group", type=int, default=2)
    ap.add_argument("--step-days", type=int, default=42)
    ap.add_argument("--total", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()

    if a.collect:
        collect(a.collect, a.out)
        return

    if a.extend:
        bp = os.path.join(RUNS, os.path.basename(a.extend.rstrip("/")),
                          "best_config.json")
        if not os.path.exists(bp):
            sys.exit(f"--extend: '{a.extend}' not found")
        base = json.load(open(bp))
        if base.get("strategy") != "metax2":
            sys.exit(f"--extend: '{a.extend}' is not a metax2 run")
        prev = [c["run"] for c in base["cand"]["components"]]
        adds = [x.strip() for x in (a.add or "").split(",") if x.strip()]
        a.runs = ",".join(dict.fromkeys(prev + adds))
        print(f"extending {os.path.basename(a.extend)}: {len(prev)} base + "
              f"{len(adds)} added components", flush=True)
    pairs = ALL_PAIRS if a.pairs == "all" else \
        [p.strip().lower() for p in a.pairs.split(",")]
    mode = "spot"
    if a.runs:
        # EXPLICIT components: any mix of pairs/timeframes, one shared mode
        groups, mode = {}, None
        for rn in [x.strip() for x in a.runs.split(",") if x.strip()]:
            p = os.path.join(RUNS, rn, "best_config.json")
            if not os.path.exists(p):
                sys.exit(f"run '{rn}' not found")
            b = json.load(open(p))
            strat = b.get("strategy") or (b.get("cand") or {}).get("strategy")
            if strat in ("metax", "metax2", "pairx", "fcfsx") or not b.get("cand"):
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
                os.path.join(RUNS, rn, "holdout_best_config.json"))                 else "best_config.json"
            groups.setdefault((coin, tf, mkt), []).append(
                (honest_growth(b) or 0.0, rn, fn, strat))
        print(f"explicit components: "
              f"{[rn for v in groups.values() for _, rn, _, _ in v]}",
              flush=True)
    else:
        groups = {(c, tf, "spot"): v
                  for (c, tf), v in mine(pairs, per_group=a.per_group).items()}
    if not groups:
        sys.exit("no components")
    # reference buckets follow the selection: single-pair selections bucket
    # by THAT pair's 3m vol; mixed selections use SOL's
    ref_coin = ({k[0] for k in groups}.pop()
                if len({k[0] for k in groups}) == 1 else "sol")
    ref_mkt = "spot" if mode == "spot" else "perp"
    os.environ.update(LAB_COIN=ref_coin, LAB_MARKET=ref_mkt, LAB_TF="3",
                      LAB_DATA_PINNED="1")
    print(f"reference buckets: {ref_coin.upper()} 3m {ref_mkt} vol terciles",
          flush=True)
    from metax_cli import merge_taken, rows_stats, search_window, bucket_arrays
    times, bucket, n_b = bucket_arrays("vol3")
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
            tabs.append(to_table(tab["trades"], times, bucket))
    if len(comps) < 2:
        sys.exit("fewer than 2 usable components")
    print(f"{len(comps)} components across "
          f"{len({(c['pair'], c['timeframe']) for c in comps})} pair/tf groups; "
          f"walk-forward…", flush=True)

    dd_gate = 0.35 if mode == "spot" else 0.50
    step = a.step_days * _DAY_NS
    t_first = min(t[0, 0] for t in tabs if len(t))
    t_last = max(t[:, 1].max() for t in tabs if len(t))
    t0 = int(np.datetime64("2025-01-01").astype("datetime64[ns]").astype("int64"))
    T = max(t0, int(t_first + 200 * _DAY_NS))
    rng = np.random.default_rng(a.seed)
    chained, picks, folds = [], [], 0
    last_exit = -np.inf
    while T < t_last - _DAY_NS:
        assign = search_window(tabs, n_b, len(comps), T, a.total, rng,
                               dd_gate=dd_gate)
        d = str(np.datetime64(int(T), "ns"))[:10]
        if assign is None:
            print(f"  fold @{d}: no feasible assignment — flat", flush=True)
            picks.append(dict(fold=d, assign=None))
        else:
            oos = merge_taken(assign, tabs, T, T + step)
            took = []
            for row in oos:
                if row[0] < last_exit:
                    continue
                last_exit = row[1]
                took.append(row)
            named = [f"{comps[x]['pair'][:3]}/{comps[x]['timeframe']}·"
                     f"{comps[x]['run'][:20]}" if x >= 0 else "—"
                     for x in assign]
            print(f"  fold @{d}: {named} -> {len(took)} trades", flush=True)
            chained += took
            picks.append(dict(fold=d, assign=[int(x) for x in assign]))
        folds += 1
        T += step
    S = rows_stats(np.array([r[:8] for r in chained])
                   if chained else np.zeros((0, 8)))
    if not S:
        print("VERDICT: NO-TRADES", flush=True)
        return
    pct = 100 * (math.exp(S["growth"]) - 1)
    verdict = "PASS" if (pct > 0 and not S["liq"] and S["maxdd"] <= 0.5) \
        else "FAIL"
    print(f"METAX2 VERDICT ({a.name}): {verdict} — chained OOS {pct:+.1f}%/mo, "
          f"dd {100*S['maxdd']:.0f}%, {S['n']} trades over {folds} folds",
          flush=True)
    cur = search_window(tabs, n_b, len(comps), int(t_last) + 1, a.total, rng,
                        dd_gate=dd_gate)
    json.dump(dict(strategy="metax2", mode=mode, method="xtf-vol3",
                   pair=(f"{ref_coin.upper()}_USDT" if len({k[0] for k in groups}) == 1 else "BASKET"),
                   market_data=ref_mkt, timeframe="multi", algo="router-xtf",
                   cand=dict(strategy="metax2", components=comps,
                             assign=([int(x) for x in cur] if cur is not None
                                     else None),
                             buckets="vol3 (reference: SOL 3m spot)",
                             fold_picks=picks,
                             live_replication="NOT YET SUPPORTED — research "
                             "artifact; panel refuses adoption"),
                   metrics=S, holdout=S, evaluated=folds * a.total,
                   generated=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "best_config.json"), "w"),
              indent=1, default=float)
    json.dump(dict(pool=[], evaluated=folds * a.total, seed_base=a.seed,
                   reservoir=[], res_seen=0, runtime_s=0),
              open(os.path.join(run_dir, "pool2.json"), "w"))
    json.dump(dict(verdict=verdict, oos_pct_mo=pct, maxdd=S["maxdd"], n=S["n"],
                   folds=folds, step_days=a.step_days,
                   note="causal: per-fold assignment from past-only trades",
                   at=time.strftime("%Y-%m-%d %H:%M")),
              open(os.path.join(run_dir, "walkforward.json"), "w"), indent=1)
    # publish the chained causal curve
    eq = 1000.0
    curve, trades, mo_map = [], [], {}
    wins = 0
    for row in chained:
        et, xt, r, mae = row[0], row[1], row[2], row[3]
        k = int(row[7])
        r = max(r, -0.999)
        eq *= (1.0 + r)
        ts = str(np.datetime64(int(et), "ns"))[:16]
        curve.append(dict(t=ts, eq=eq))
        wins += r > 0
        c = comps[k]
        trades.append(dict(entry_t=ts, exit_t=str(np.datetime64(int(xt), "ns"))[:16],
                           dir="long", entry=0.0, exit=0.0,
                           net=round(eq * r / (1 + r), 2), mae=float(mae),
                           reason=f"{c['pair'][:3]}/{c['timeframe']}·{c['strategy']}",
                           lev=1.0))
        mo_map[ts[:7]] = mo_map.get(ts[:7], 1.0) * (1.0 + r)
    months = max(len(mo_map), 1)
    entry = dict(name=f"{a.name}_router_full",
                 stats=dict(months=months, final_eq=eq, total_mult=eq / 1000.0,
                            monthly_growth_pct=100 * ((eq / 1000.0) ** (1 / months) - 1),
                            liq=False, maxdd_mtm=S["maxdd"], n=len(trades),
                            tpm=len(trades) / months, sl_hits=0,
                            win=wins / max(len(trades), 1)),
                 curve=curve[-400:], trades=trades[-400:],
                 monthly=[dict(month=m, ret_pct=100 * (v - 1))
                          for m, v in sorted(mo_map.items())],
                 open_positions=[], gap_mode="skip_contaminated",
                 gap_handling=dict(threshold_min=1000, n_segments=0,
                                   skipped_gaps=[],
                                   note="cross-timeframe router: components "
                                        "simulated on their own datasets"),
                 strategy="metax2", mode=mode, method="xtf-vol3",
                 pair=(f"{ref_coin.upper()}_USDT" if len({k[0] for k in groups}) == 1 else "BASKET"),
                 market_data=ref_mkt, timeframe="multi",
                 kind="cross-timeframe router walk-forward (CAUSAL: per-fold "
                      "assignment from past-only data)",
                 config=None, created=time.strftime("%Y-%m-%d %H:%M"))
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
    print(f"published '{entry['name']}'", flush=True)


if __name__ == "__main__":
    main()
