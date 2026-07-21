#!/usr/bin/env python3
"""MetaX — a regime ROUTER that combines proven strategies into one.

The market is bucketed per bar (volatility terciles, trend terciles, their
9-way cross, or calendar month — the latter flagged as seasonal-memorization
research). Each bucket is assigned ONE component strategy (or none). A
component's trades count only when its entry bar falls in a bucket it owns,
and a single-position arbiter (first come, full equity, one slot — exactly
what the live trader can execute) merges the streams into one equity curve.

Components keep their OWN full parameter sets (leverage, targets, stops,
cooldowns): the router only decides who may open a trade, never how they
trade. Live replication: run every component's signal state machine
virtually, mirror real orders only for the bucket owner (documented in the
saved config).

Phase 1 (default): components frozen to the best cross-era survivors of the
c1/c2/c3 campaigns; only the assignment is searched (small, honest space).
Phase 2 (joint refine of component params under the winning assignment) runs
as the meta campaign's wave 2 — see campaign.py.

Honesty: score on ALTERNATING 21-day blocks (train = even blocks), report
holdout (odd blocks) separately; full-history publish to the Backtests page.

Usage:
  python3 metax_cli.py --mode lev --buckets vt9 --name camp_c4_lev_vt9
  python3 metax_cli.py --refine runs/camp_c4_lev_vt9 --iters 400
"""
import _bootstrap as B
import argparse, json, math, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
FUT_COMM, SPOT_COMM = 0.0004, 0.0005
BLOCK_DAYS = 21
MAX_HOLD_D = 7.0
MAX_DD = 0.55

import backtest_cli as BT


# ---------------- component mining ----------------
def mine_components(mode, max_per_family=2, cap=8):
    """The best cross-era survivors on disk for this mode: holdout-positive,
    not liquidated, holds <= 7d. Campaign c3/c2 runs outrank c1/manual."""
    cands = []
    for d in sorted(os.listdir(RUNS)):
        for fn in ("holdout_best_config.json", "best_config.json"):
            p = os.path.join(RUNS, d, fn)
            if not os.path.exists(p):
                continue
            try:
                b = json.load(open(p))
            except Exception:
                continue
            if b.get("mode") != mode or not b.get("cand"):
                continue
            h = b.get("holdout") or {}
            if fn == "best_config.json" and b.get("holdout_best"):
                h2 = (b["holdout_best"] or {}).get("holdout") or {}
                if (h2.get("growth") or -9) > (h.get("growth") or -9):
                    h = h2
            if not h or h.get("liq") or (h.get("growth") or 0) <= 0:
                continue
            if (h.get("max_hold_days") or 0) > MAX_HOLD_D:
                continue
            era_bonus = 0.02 if d.startswith(("camp_c3", "camp_c2")) else 0.0
            cands.append(dict(run=d, file=fn, path=p,
                              strategy=b.get("strategy"),
                              score=(h["growth"] + era_bonus),
                              holdout_pct=round(100 * (math.exp(h["growth"]) - 1), 1)))
            break   # one config per run dir (holdout_best preferred)
    cands.sort(key=lambda c: -c["score"])
    out, per_fam = [], {}
    for c in cands:
        fam = c["strategy"]
        if per_fam.get(fam, 0) >= max_per_family or len(out) >= cap:
            continue
        per_fam[fam] = per_fam.get(fam, 0) + 1
        out.append(c)
    return out


# ---------------- bucket features ----------------
def bucket_arrays(buckets):
    """(times_ns sorted, bucket_id per bar, n_buckets) across all segments."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE),
                                    "adaptive_trader", "research2"))
    import wf2 as W
    G = W.load_globals(("v6",))
    segs = G["v6"][0]                       # [(pre, f), ...]
    times = np.concatenate([np.asarray(pre["t"], dtype="datetime64[ns]").astype("int64")
                            for pre, f in segs])
    if buckets == "month12":
        dt = times.astype("datetime64[ns]")
        b = (dt.astype("datetime64[M]").astype(int) % 12)   # 0 = January
        return times, b.astype(np.int32), 12
    method = {"vol3": "vol3", "trend3": "trend3", "vt9": "volXtrend9"}[buckets]
    regs = G["regimes_v6"][method]
    b = np.concatenate([np.asarray(r, dtype=np.int32) for r in regs])
    return times, b, int(b.max()) + 1


# ---------------- component trade tables ----------------
def component_trades(comp, times, bucket, mode):
    """Full-history backtest of one component -> vectorized trade table."""
    e = BT.run_single(comp["path"])
    rows = []
    comm = FUT_COMM if mode == "lev" else SPOT_COMM
    for t in e["trades"]:
        try:
            et = np.datetime64(t["entry_t"]).astype("datetime64[ns]").astype("int64")
            xt = np.datetime64(t["exit_t"]).astype("datetime64[ns]").astype("int64")
        except Exception:
            continue
        lev = float(t.get("lev") or 1.0)
        dr = 1.0 if t["dir"] == "long" else -1.0
        move = dr * (t["exit"] / t["entry"] - 1.0)
        r = move * lev - comm * lev * (1.0 + t["exit"] / t["entry"])
        liq = 1.0 if str(t.get("reason", "")).startswith("liq") else 0.0
        mae_eq = float(t.get("mae") or 0.0) * lev          # worst intra-trade equity move
        i = int(np.searchsorted(times, et))
        if i >= len(times):
            i = len(times) - 1
        rows.append((et, xt, r, mae_eq, liq, bucket[i],
                     (xt - et) / 86_400_000_000_000.0))
    arr = np.array(rows, dtype=np.float64) if rows else np.zeros((0, 7))
    return arr   # cols: entry_ns, exit_ns, ret, mae_eq, liq, bucket, hold_days


def build_tables(comps, times, bucket, mode):
    tabs = []
    for k, c in enumerate(comps):
        a = component_trades(c, times, bucket, mode)
        print(f"  component {k}: {c['run'][:48]} ({c['strategy']}) "
              f"{len(a)} trades, holdout {c['holdout_pct']:+.1f}%/mo", flush=True)
        tabs.append(a)
    return tabs


# ---------------- router evaluation ----------------
def eval_assign(assign, tabs, collect=False):
    """Single-slot merge of the assigned trades; classic score on even
    21-day blocks, holdout metrics on odd blocks."""
    take = []
    for k, tab in enumerate(tabs):
        if len(tab) == 0:
            continue
        m = np.isin(tab[:, 5].astype(int), np.where(np.asarray(assign) == k)[0])
        if m.any():
            t = tab[m]
            take.append(np.column_stack([t, np.full(len(t), k)]))
    if not take:
        return None
    T = np.vstack(take)
    T = T[np.argsort(T[:, 0])]
    eq_tr, eq_ho = 1000.0, 1000.0
    peak_tr, peak_ho = 1000.0, 1000.0
    dd_tr, dd_ho = 0.0, 0.0
    mo_tr, mo_ho = {}, {}
    last_exit = -np.inf
    n_tr = n_ho = 0
    max_hold = 0.0
    liq_hit = False
    taken_rows = []
    for row in T:
        et, xt, r, mae_eq, liq, bkt, hold, k = row
        if et < last_exit:                      # slot busy
            continue
        last_exit = xt
        blk = int(et // (BLOCK_DAYS * 86_400_000_000_000))
        is_train = (blk % 2 == 0)
        mo = int(et // (30.44 * 86_400_000_000_000))
        max_hold = max(max_hold, hold)
        if liq:
            liq_hit = True
        r = max(r, -0.999)
        if is_train:
            trough = eq_tr * (1.0 + min(mae_eq, 0.0))
            eq_tr *= (1.0 + r)
            peak_tr = max(peak_tr, eq_tr)
            dd_tr = max(dd_tr, 1 - min(trough, eq_tr) / peak_tr)
            mo_tr[mo] = mo_tr.get(mo, 0.0) + math.log(max(1e-9, 1.0 + r))
            n_tr += 1
        else:
            trough = eq_ho * (1.0 + min(mae_eq, 0.0))
            eq_ho *= (1.0 + r)
            peak_ho = max(peak_ho, eq_ho)
            dd_ho = max(dd_ho, 1 - min(trough, eq_ho) / peak_ho)
            mo_ho[mo] = mo_ho.get(mo, 0.0) + math.log(max(1e-9, 1.0 + r))
            n_ho += 1
        if collect:
            taken_rows.append(row)
    span_ns = (T[-1, 1] - T[0, 0]) if len(T) else 1
    months_half = max(span_ns / (30.44 * 86_400_000_000_000) / 2.0, 0.5)

    def stats(mo_map, n, eq, dd):
        if not mo_map:
            return None
        g = np.array(list(mo_map.values()))
        score = float(g.mean() - 0.25 * g.std())
        return dict(growth=float(g.mean()), score=score, eq=eq, maxdd=dd,
                    n=n, months=months_half, tpm=n / months_half,
                    max_hold_days=max_hold, liq=liq_hit)
    tr_s, ho_s = stats(mo_tr, n_tr, eq_tr, dd_tr), stats(mo_ho, n_ho, eq_ho, dd_ho)
    if collect:
        return tr_s, ho_s, (np.array(taken_rows) if taken_rows else np.zeros((0, 8)))
    return tr_s, ho_s


def feasible(tr_s):
    return (tr_s and not tr_s["liq"] and tr_s["n"] >= 20 and tr_s["tpm"] >= 2
            and tr_s["maxdd"] <= MAX_DD and tr_s["max_hold_days"] <= MAX_HOLD_D)


# ---------------- assignment search ----------------
def search(tabs, n_buckets, n_comps, total, rng):
    best = None   # (score, assign, tr_s, ho_s)
    pool = []
    evals = 0
    def consider(a):
        nonlocal best, evals
        evals += 1
        res = eval_assign(a, tabs)
        if res is None:
            return
        tr_s, ho_s = res
        if not feasible(tr_s):
            return
        pool.append((tr_s["score"], tuple(a)))
        if best is None or tr_s["score"] > best[0]:
            best = (tr_s["score"], list(a), tr_s, ho_s)
    # seed: every single-component blanket + random assignments
    for k in range(n_comps):
        consider([k] * n_buckets)
    while evals < total:
        if pool and rng.random() < 0.7:
            pool.sort(key=lambda x: -x[0])
            parents = pool[:24]
            a = list(parents[rng.integers(0, len(parents))][1])
            b = list(parents[rng.integers(0, len(parents))][1])
            child = [a[i] if rng.random() < 0.5 else b[i] for i in range(n_buckets)]
            for i in range(n_buckets):
                if rng.random() < 0.15:
                    child[i] = int(rng.integers(-1, n_comps))
            consider(child)
        else:
            consider([int(rng.integers(-1, n_comps)) for _ in range(n_buckets)])
    return best, evals


# ---------------- publish ----------------
def publish_backtest(name, mode, buckets, comps, assign, taken, tr_s, ho_s):
    """Merged full-history equity into dashboard/backtests.js (same schema)."""
    eq = 1000.0
    curve, trades, mo_map = [], [], {}
    wins = 0
    for row in taken:
        et, xt, r, mae_eq, liq, bkt, hold, k = row
        r = max(r, -0.999)
        eq *= (1.0 + r)
        ts = str(np.datetime64(int(et), "ns"))[:16]
        xs = str(np.datetime64(int(xt), "ns"))[:16]
        curve.append(dict(t=ts, eq=eq))
        if r > 0:
            wins += 1
        c = comps[int(k)]
        trades.append(dict(entry_t=ts, exit_t=xs, dir="long", entry=0.0, exit=0.0,
                           net=round(eq * r / (1 + r), 2), mae=float(mae_eq),
                           reason=f"{c['strategy']}·b{int(bkt)}", lev=1.0))
        mo = ts[:7]
        mo_map[mo] = mo_map.get(mo, 1.0) * (1.0 + r)
    months = max(len(mo_map), 1)
    dd = max((tr_s or {}).get("maxdd", 0), (ho_s or {}).get("maxdd", 0))
    entry = dict(
        name=f"{name}_router_full",
        stats=dict(months=months, final_eq=eq, total_mult=eq / 1000.0,
                   monthly_growth_pct=100 * ((eq / 1000.0) ** (1 / months) - 1),
                   liq=bool((tr_s or {}).get("liq") or (ho_s or {}).get("liq")),
                   maxdd_mtm=dd, n=len(trades), tpm=len(trades) / months,
                   sl_hits=0, win=(wins / max(len(trades), 1))),
        curve=curve, trades=trades[-400:],
        monthly=[dict(month=m, ret_pct=100 * (v - 1)) for m, v in sorted(mo_map.items())],
        open_positions=[], gap_mode="inherited from components",
        strategy="metax", mode=mode, method=buckets,
        kind="full-history (router merge)",
        config=dict(assign=assign, components=[c["run"] for c in comps]),
        created=time.strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(DASH, "backtests.js")
    txt = open(p).read()
    entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
    entries = [e for e in entries if e.get("name") != entry["name"]] + [entry]
    with open(p, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")
    print(f"published '{entry['name']}' ({len(entries)} entries)", flush=True)


# ---------------- phase 2: joint refine of component params ----------------
def refine(run_dir, iters, seed=11):
    """Hill-climb component parameters UNDER the frozen winning assignment.
    Each step mutates one component with its family's native mutation ops,
    re-backtests it, re-merges, and keeps the change only if the router's
    train score improves. Holdout stays untouched as the judge."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE),
                                    "adaptive_trader", "research2"))
    import wf2 as W
    import optimizer2 as O
    cfgp = os.path.join(run_dir, "best_config.json")
    meta = json.load(open(cfgp))
    cand = meta["cand"]
    mode, buckets, assign = meta["mode"], meta["method"], cand["assign"]
    comps = [dict(run=c["run"], file=c["file"], strategy=c["strategy"],
                  path=os.path.join(RUNS, c["run"], c["file"]))
             for c in cand["components"]]
    for c in comps:
        c["cfg"] = json.load(open(c["path"]))
        c["holdout_pct"] = 0.0
    times, bucket, n_b = bucket_arrays(buckets)
    space_all = json.load(open(os.path.join(HERE, "param_space.json")))
    tmp = os.path.join(run_dir, "_refine_tmp.json")
    tabs = build_tables(comps, times, bucket, mode)
    cur = eval_assign(assign, tabs)
    if cur is None or cur[0] is None:
        print("refine: assignment no longer evaluates — aborting", flush=True)
        return
    best_score = cur[0]["score"]
    print(f"refine start: train score {best_score:.3f}, {iters} iterations",
          flush=True)
    rng = np.random.default_rng(seed)
    used = sorted({a for a in assign if a >= 0})
    accepted = 0
    for it in range(iters):
        k = int(used[rng.integers(0, len(used))])
        c = comps[k]
        strat = c["cfg"].get("strategy") or c["cfg"]["cand"].get("strategy")
        base_cand = json.loads(json.dumps(c["cfg"]["cand"]))
        sp_key = f"{strat}@spot" if (mode == "spot"
                                     and f"{strat}@spot" in space_all) else strat
        space = space_all.get(sp_key) or {}
        try:
            if strat in ("v7", "prime7"):
                mut = O.mutate(rng, base_cand, space or O.load_space(), mode,
                               p_cont=0.15, sigma=0.05)
                if base_cand.get("lev_stops"):
                    mut["lev_stops"] = True
            else:
                mut = W.mutate_flat(rng, base_cand, mode, space,
                                    p_cont=0.15, sigma=0.05)
        except Exception as e:
            continue
        trial_cfg = dict(c["cfg"], cand=mut)
        json.dump(trial_cfg, open(tmp, "w"), default=float)
        try:
            trial = dict(c, path=tmp)
            new_tab = component_trades(trial, times, bucket, mode)
        except Exception:
            continue
        old_tab = tabs[k]
        tabs[k] = new_tab
        res = eval_assign(assign, tabs)
        ok = (res is not None and res[0] is not None and feasible(res[0])
              and res[0]["score"] > best_score)
        if ok:
            best_score = res[0]["score"]
            c["cfg"] = trial_cfg
            accepted += 1
            print(f"  it {it}: comp {k} ({strat}) accepted -> "
                  f"score {best_score:.3f}", flush=True)
        else:
            tabs[k] = old_tab
        if (it + 1) % 50 == 0:
            print(f"  …{it+1}/{iters}, {accepted} accepted, "
                  f"score {best_score:.3f}", flush=True)
    tr_s, ho_s, taken = eval_assign(assign, tabs, collect=True)
    cand["components"] = [dict(run=c["run"], file=c["file"],
                               strategy=c["strategy"],
                               cand=c["cfg"]["cand"]) for c in comps]
    meta.update(metrics=tr_s, holdout=ho_s, refined=accepted,
                generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(meta, open(cfgp, "w"), indent=1, default=float)
    if os.path.exists(tmp):
        os.remove(tmp)
    name = os.path.basename(run_dir.rstrip("/"))
    publish_backtest(name + "_refined", mode, buckets,
                     comps, assign, taken, tr_s, ho_s)
    if ho_s:
        print(f"refine done: {accepted} accepted | holdout "
              f"{100*(math.exp(ho_s['growth'])-1):+.1f}%/mo "
              f"dd {100*ho_s['maxdd']:.0f}%", flush=True)


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refine", default=None,
                    help="run dir of an existing metax run: joint-refine its "
                         "component parameters under the frozen assignment")
    ap.add_argument("--iters", type=int, default=400)
    ap.add_argument("--mode", default=None, choices=["lev", "spot"])
    ap.add_argument("--buckets", default="vt9",
                    choices=["vol3", "trend3", "vt9", "month12"])
    ap.add_argument("--name", default=None)
    ap.add_argument("--total", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.refine:
        rd = args.refine if os.path.isabs(args.refine) \
            else os.path.join(HERE, args.refine)
        refine(rd, args.iters, args.seed)
        return
    if not args.mode or not args.name:
        sys.exit("--mode and --name are required (or use --refine)")
    if args.buckets == "month12":
        print("NOTE: month12 = calendar routing. With ~2 samples per month in "
              "the data this measures seasonal MEMORIZATION as much as edge — "
              "flagged research, not an adoption candidate.", flush=True)
    run_dir = os.path.join(RUNS, args.name)
    os.makedirs(run_dir, exist_ok=True)
    print(f"mining components ({args.mode})…", flush=True)
    comps = mine_components(args.mode)
    if len(comps) < 2:
        print("fewer than 2 qualifying components — nothing to route", flush=True)
        sys.exit(1)
    times, bucket, n_b = bucket_arrays(args.buckets)
    print(f"{len(comps)} components, {n_b} buckets ({args.buckets})", flush=True)
    tabs = build_tables(comps, times, bucket, args.mode)
    rng = np.random.default_rng(args.seed)
    t0 = time.time()
    best, evals = search(tabs, n_b, len(comps), args.total, rng)
    print(f"searched {evals} assignments in {time.time()-t0:.0f}s", flush=True)
    if best is None:
        print("no feasible assignment (gates: no-liq, dd<=55%, tpm>=2, "
              "hold<=7d)", flush=True)
        json.dump(dict(pool=[], evaluated=evals, seed_base=args.seed,
                       reservoir=[], res_seen=0, runtime_s=time.time() - t0),
                  open(os.path.join(run_dir, "pool2.json"), "w"))
        sys.exit(0)
    score, assign, tr_s, ho_s = best
    tr2, ho2, taken = eval_assign(assign, tabs, collect=True)
    named = {i: (comps[a]["run"][:40] if a >= 0 else "—")
             for i, a in enumerate(assign)}
    print("winning assignment:", json.dumps(named, indent=1), flush=True)
    if ho_s:
        hp = 100 * (math.exp(ho_s["growth"]) - 1)
        print(f"train score {score:.3f} | holdout {hp:+.1f}%/mo "
              f"dd {100*ho_s['maxdd']:.0f}% tpm {ho_s['tpm']:.1f}", flush=True)
    else:
        print(f"train score {score:.3f} | holdout n/a", flush=True)
    cand = dict(strategy="metax", mode=args.mode, buckets=args.buckets,
                assign=assign,
                components=[dict(run=c["run"], file=c["file"],
                                 strategy=c["strategy"]) for c in comps],
                live_replication=("run every component's signal state machine "
                                  "virtually; mirror real orders only when the "
                                  "entry bar's bucket is assigned to that "
                                  "component and the single slot is free"))
    out = dict(strategy="metax", mode=args.mode, method=args.buckets,
               algo="router", per_regime=True, cand=cand,
               metrics=tr_s, holdout=ho_s,
               seasonal_flag=(args.buckets == "month12"),
               evaluated=evals, generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open(os.path.join(run_dir, "best_config.json"), "w"),
              indent=1, default=float)
    json.dump(dict(pool=[], evaluated=evals, seed_base=args.seed, reservoir=[],
                   res_seen=0, runtime_s=time.time() - t0),
              open(os.path.join(run_dir, "pool2.json"), "w"))
    publish_backtest(args.name, args.mode, args.buckets, comps, assign,
                     taken, tr2, ho2)

if __name__ == "__main__":
    main()
