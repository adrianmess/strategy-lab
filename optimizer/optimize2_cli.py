#!/usr/bin/env python3
"""Unified optimizer CLI — parameter search for ALL strategies
(V7 full-param, Solana Prime, V6, ScalpX) with algorithm choice
(genetic / random / refine), per-regime specialist sets (V7), train/holdout
splits, seeding from backtests, and resumable pools.

Examples:
  # genetic search, per-regime specialists on volatility terciles, 8 cores, 4 hours:
  python3 optimize2_cli.py --algo genetic --mode lev --method vol3 \
      --procs 8 --hours 4 --name big_lev_vol3

  # random baseline on single set:
  python3 optimize2_cli.py --algo random --mode spot --method none \
      --procs 8 --total 50000 --name spot_rand

  # polish an existing run:
  python3 optimize2_cli.py --algo refine --resume-from runs/big_lev_vol3 \
      --mode lev --method vol3 --procs 8 --hours 1 --name big_lev_vol3_polish

Notes:
  --train-end lets you hold out recent data (e.g. 2025-09-01) to verify on
  unseen data afterwards (backtest with --oos-start).
  Parameter ranges/menus: edit optimizer/param_space.json (or via the site).
Resumable: same --name continues. Output: runs/<name>/best_config.json
"""
import _bootstrap as B
import argparse, json, os, signal, time
import multiprocessing as mp
import numpy as np

STOP = {"flag": False}

def _request_stop(signum, frame):
    if not STOP["flag"]:
        STOP["flag"] = True
        print("STOP requested — finishing the current generation, then running "
              "the finalize step (holdout evaluation)...", flush=True)


def worker(args):
    kind, payload = args
    rng = np.random.default_rng(payload["seed"])
    space = payload["space"]
    strategy = payload.get("strategy", "v7")
    if strategy in ("v7", "prime7"):   # prime7 = prime on the full-param v7 engine
        import optimizer2 as O
        O.load_g3()
        if kind == "random":
            res = O.batch_random(rng, space, payload["R"], payload["mode"],
                                 payload["method"], payload["n"],
                                 payload["t0"], payload["t1"], payload["per_regime"],
                                 max_dd=payload["max_dd"], alt=payload["alt"],
                                 max_hold=payload["max_hold"], gap_mode=payload["gap_mode"],
                                 scoring=payload["scoring"])
        elif kind == "offspring":
            res = O.batch_offspring(rng, space, payload["mode"], payload["method"],
                                    payload["parents"], payload["n"],
                                    payload["t0"], payload["t1"],
                                    max_dd=payload["max_dd"], alt=payload["alt"],
                                    max_hold=payload["max_hold"], gap_mode=payload["gap_mode"],
                                 scoring=payload["scoring"])
        elif kind == "refine":
            res = O.batch_refine(rng, space, payload["mode"], payload["method"],
                                 payload["seed_cand"], payload["n"],
                                 payload["t0"], payload["t1"],
                                 max_dd=payload["max_dd"], alt=payload["alt"],
                                 max_hold=payload["max_hold"], gap_mode=payload["gap_mode"],
                                 scoring=payload["scoring"])
        else:
            res = []
        for _s, _c, _m in res:
            _c["strategy"] = strategy
        return _apply_anchor(res, payload, space, strategy)
    # flat-candidate strategies (prime / v6 / scalpx) via the wf2 engine
    import wf2 as W
    G = W.load_globals(("v6",) if strategy == "prime" else (strategy,))
    R = G["nreg"][payload["method"]]
    strip = lambda res: [(s, c, {k: v for k, v in m.items() if k != "trades"})
                         for s, c, m in res]
    if kind == "offspring":
        return _apply_anchor(strip(W.batch_offspring_flat(rng, payload["parents"], payload["mode"],
                                            space, None, R, payload["method"],
                                            payload["n"], payload["t0"], payload["t1"],
                                            max_dd=payload["max_dd"], alt=payload["alt"],
                                            max_hold=payload["max_hold"],
                                            gap_mode=payload["gap_mode"],
                                            scoring=payload["scoring"])), payload, space, strategy)
    if kind == "refine":
        return _apply_anchor(strip(W.batch_refine_flat(rng, payload["seed_cand"], payload["mode"],
                                         space, payload["method"],
                                         payload["n"], payload["t0"], payload["t1"],
                                         max_dd=payload["max_dd"], alt=payload["alt"],
                                         max_hold=payload["max_hold"],
                                         gap_mode=payload["gap_mode"],
                                         scoring=payload["scoring"])), payload, space, strategy)
    sampler = {"v6": W.sample_v6, "prime": W.sample_prime,
               "scalpx2": W.sample_scalpx2}.get(strategy, W.sample_scalpx)
    out = []
    for _ in range(payload["n"]):
        c = sampler(rng, R, payload["mode"], space)
        m = W.eval_config(c, payload["method"], payload["mode"],
                          payload["t0"], payload["t1"], alt=payload["alt"],
                          gap_mode=payload["gap_mode"], scoring=payload["scoring"])
        if W.feasible(m, payload["mode"], cand=c, max_dd=payload["max_dd"],
                      max_hold=payload["max_hold"]):
            out.append((m["score"], c, {k: v for k, v in m.items() if k != "trades"}))
    return _apply_anchor(out, payload, space, strategy)


def _apply_anchor(res, payload, space, strategy):
    a = payload.get("anchor_cand")
    s = payload.get("anchor_strength") or 0.0
    if not a or s <= 0 or not res:
        return res
    return [(sc - s * anchor_distance(c, a, space, strategy), c, m)
            for sc, c, m in res]


def build_anchor_defaults(strategy, mode, R, space):
    """The strategy's stored live-default parameters as a candidate."""
    if strategy in ("v7", "prime7"):
        from engine3 import DEFAULTS3
        base = dict(DEFAULTS3)
        if strategy == "prime7":
            import wf2 as W
            for k, v in W.PRIME_BASE.items():
                if k in base and isinstance(v, (int, float)):
                    base[k] = float(v)
            base.update(eXL=0.0, eXS=0.0, requireHistPos=0.0)
        for k, spec in (space.get("flags") or {}).items():
            if isinstance(spec, dict) and spec.get("fixed") is not None:
                base[k] = float(spec["fixed"])
        if mode == "spot":
            base.update(leverage=1.0, eS3=0.0, eXS=0.0)
        return dict(strategy=strategy, mode=mode,
                    regs=[dict(base) for _ in range(R)])
    if strategy == "prime":
        import wf2 as W
        P = W.PRIME_BASE
        c = dict(strategy="prime", tv=0,
                 zL=[P["macdValPctLong"]] * R, zS=[P["macdValPctShort"]] * R,
                 rsiL=[P["rsiValLong"]] * R, rsiS=[P["rsiValShort"]] * R,
                 bbL=[P["bbValLong"]] * R, bbS=[P["bbValShort"]] * R,
                 ptL=[P["ptLong"]] * R, a1L=[P["apt1Long"]] * R, a2L=[P["apt2Long"]] * R,
                 d1L=[P["dur1Long"]] * R, d2L=[P["dur2Long"]] * R,
                 ptS=[P["ptShort"]] * R, a1S=[P["apt1Short"]] * R, a2S=[P["apt2Short"]] * R,
                 d1S=[P["dur1Short"]] * R, d2S=[P["dur2Short"]] * R,
                 cdPL=[P["cdPctLong"]] * R, cdTL=[float(P["cdPeriodLong"])] * R,
                 cdPS=[P["cdPctShort"]] * R, cdTS=[float(P["cdPeriodShort"])] * R,
                 eL3=[1.0] * R, eS3=[0.0 if mode == "spot" else 1.0] * R,
                 lev=[1.0 if mode == "spot" else float(P.get("leverage", 2.5))] * R,
                 sl=(0.10 if mode == "spot" else 0.0))
        return c
    if strategy in ("scalpx", "scalpx2"):
        from scalp_engine import SCALP_DEFAULTS
        D = SCALP_DEFAULTS
        c = dict(strategy=strategy,
                 tpL=[D["tpLong"]] * R, tpS=[D["tpShort"]] * R,
                 rsiOB=[D["rsiOB"]] * R, rsiOS=[D["rsiOS"]] * R,
                 useCvd=[1.0] * R, useEma=[1.0] * R,
                 eL=[1.0] * R, eS=[0.0 if mode == "spot" else 1.0] * R,
                 lev=[1.0 if mode == "spot" else float(D.get("leverage", 1.0))] * R,
                 sl=(0.05 if mode == "spot" else 0.05),
                 slOn=(1.0 if mode == "spot" else 0.0))
        if strategy == "scalpx2":
            from scalp_engine import SCALP2_DEFAULT_IDX as I
            c.update(vR=[float(I["rsi"])] * R, vC=[float(I["cvd"])] * R,
                     vP=[float(I["poc"])] * R, vE=[float(I["emaS"])] * R)
        return c
    raise SystemExit(f"--anchor defaults: {strategy} has no stored live defaults — "
                     f"anchor to a published backtest instead.")


def anchor_distance(c, anchor, space, strategy):
    """Mean normalized parameter distance between candidate and anchor (0 = identical)."""
    tot, n = 0.0, 0
    cont = space.get("continuous") or {}
    if strategy in ("v7", "prime7"):
        for ra, rb in zip(anchor.get("regs", []), c.get("regs", [])):
            for k, spec in cont.items():
                lo, hi = spec.get("range", (0, 0))
                if hi > lo and k in ra and k in rb:
                    tot += min(1.0, abs(float(rb[k]) - float(ra[k])) / (hi - lo)); n += 1
            for k in (space.get("menus") or {}):
                if k in ra and k in rb:
                    tot += 0.0 if ra[k] == rb[k] else 1.0; n += 1
            for k in (space.get("flags") or {}):
                if k in ra and k in rb:
                    tot += abs(float(rb[k]) - float(ra[k])); n += 1
        return tot / max(n, 1)
    from wf2 import FLAT_KEYMAP
    km = FLAT_KEYMAP.get(strategy, {})
    for k, av in anchor.items():
        if k in ("strategy", "tv") or k not in c:
            continue
        cv = c[k]
        if isinstance(av, list) and isinstance(cv, list):
            spec = cont.get(km.get(k, k))
            for a2, c2 in zip(av, cv):
                if spec and spec["range"][1] > spec["range"][0]:
                    lo, hi = spec["range"]
                    tot += min(1.0, abs(float(c2) - float(a2)) / (hi - lo))
                else:
                    tot += min(1.0, abs(float(c2) - float(a2)) / (abs(float(a2)) + 1e-9))
                n += 1
        elif isinstance(av, (int, float)) and isinstance(cv, (int, float)):
            tot += min(1.0, abs(float(cv) - float(av)) / (abs(float(av)) + 1e-9)); n += 1
    return tot / max(n, 1)


def _scoring(args):
    """classic -> None so evaluation takes the pre-existing, untouched code path."""
    return None if args.scoring == "classic" else args.scoring


def reservoir_add(res, seen, items, rng, cap=300):
    """Uniform reservoir sample over every feasible candidate ever produced —
    keeps generalizers that the score-ranked elite pool would evict."""
    for it in items:
        seen[0] += 1
        if len(res) < cap:
            res.append(it)
        else:
            j = int(rng.integers(0, seen[0]))
            if j < cap:
                res[j] = it
    return res


def parse_lockbox(s):
    """'..2024-11-01,2025-07-01..' -> [(None,'2024-11-01'), ('2025-07-01',None)].
    A bare date ('2025-09-01') means from that date onward."""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            a, b = part.split("..", 1)
        else:
            a, b = part, ""   # bare date = lockbox from that date to the end
        a, b = (a.strip() or None), (b.strip() or None)
        for d in (a, b):
            if d is not None:
                try:
                    np.datetime64(d)
                except Exception:
                    raise SystemExit(
                        f"--lockbox: '{d}' is not a date. Use YYYY-MM-DD ranges like "
                        f"'2025-09-01..' or '..2024-11-01,2025-07-01..' (comma-separated, "
                        f"open sides allowed; a bare date means 'from that date onward').")
        out.append((a, b))
    return out


def complement_ranges(lockboxes):
    """The searchable ranges = everything the lockboxes don't cover."""
    if not lockboxes:
        return None
    lbs = sorted(lockboxes, key=lambda r: (r[0] is not None, r[0] or ""))
    out, prev_end = [], None
    for a, b in lbs:
        if a is None:
            prev_end = b
            continue
        out.append((prev_end, a))
        prev_end = b
    if prev_end is not None:
        out.append((prev_end, None))
    out = [r for r in out if not (r[0] is None and r[1] is None)]
    return out or None


def run_crossfit(args, space, R, per_regime, flat, anchor_cand=None):
    """Cross-fit: search A on even fold-blocks, search B on odd, cross-score every
    pool candidate on its opposite fold, breed the mutual survivors on the full
    (non-lockbox) region, then judge finalists once on each lockbox."""
    fold = args.holdout_days
    lockboxes = parse_lockbox(args.lockbox)
    ranges = complement_ranges(lockboxes)
    print(f"CROSS-FIT | fold {fold:g}d | lockboxes {lockboxes or 'none'} | "
          f"search ranges {ranges or 'all data'}", flush=True)
    if flat:
        import wf2 as W
        def ev(c, alt):
            return W.eval_config(c, args.method, args.mode, None, None,
                                 alt=alt, gap_mode=args.gap_mode, scoring=_scoring(args))
        def feas(m, c):
            return W.feasible(m, args.mode, cand=c, max_dd=args.max_dd,
                              max_hold=args.max_hold_days)
    else:
        import optimizer2 as O
        def ev(c, alt):
            return O.eval3(c, args.method, alt=alt, gap_mode=args.gap_mode,
                           scoring=_scoring(args))
        def feas(m, c):
            return O.feasible3(m, args.mode, cand=c, max_dd=args.max_dd,
                               max_hold=args.max_hold_days)

    total_budget = args.total
    hours_budget = args.hours
    t0_session = time.time()
    state = dict(evaluated=0, seed_base=0)

    def write_prog(base, span, phase_pct, label):
        el = time.time() - t0_session
        pct = min(1.0, base + span * min(1.0, phase_pct))
        if total_budget:
            rate = state["evaluated"] / max(el, 1e-9)
            eta = max(0.0, (total_budget - state["evaluated"]) / max(rate, 1e-9))
        else:
            eta = max(0.0, hours_budget * 3600 - el)
        json.dump(dict(pct=pct, eta_s=eta, elapsed_s=el, phase=label,
                       evaluated_session=state["evaluated"],
                       budget=(total_budget or hours_budget),
                       budget_type=("evaluations" if total_budget else "hours"),
                       updated=time.time()),
                  open("progress.json", "w"))

    def stopped():
        return STOP["flag"] or os.path.exists("stop.flag")

    def search_phase(p, pool_file, alt, share, prog_base, label, seed_pool=None,
                     cross_alt=None):
        """One genetic search under the given window spec; returns (pool, reservoir).
        cross_alt: every 5 generations, evict pool candidates that liquidate on the
        opposite fold (legitimate inside cross-fit — the lockbox stays the judge)."""
        pool = list(seed_pool or [])
        reservoir, res_seen = [], [0]
        _rr = np.random.default_rng(777)
        cross_ok = {}   # cand-key -> bool, avoid re-checking
        ev_target = int((total_budget or 10**12) * share)
        t_end = t0_session + (hours_budget * 3600 * (prog_base + share)
                              if hours_budget else 10**12)
        done = 0
        gen = 0
        while (time.time() < t_end and done < ev_target and not stopped()):
            gen += 1
            payload = dict(space=space, R=R, mode=args.mode, method=args.method,
                           n=args.batch, t0=None, t1=None,
                           per_regime=per_regime, strategy=args.strategy,
                           max_dd=args.max_dd, max_hold=args.max_hold_days,
                           gap_mode=args.gap_mode, alt=alt, scoring=_scoring(args),
                           anchor_cand=anchor_cand, anchor_strength=args.anchor_strength)
            if len(pool) < 8:
                jobs = [("random", dict(payload, seed=state["seed_base"] + k))
                        for k in range(args.procs)]
            else:
                parents = [c for _, c, _ in pool[:24]]
                jobs = [("offspring", dict(payload, parents=parents,
                                           seed=state["seed_base"] + k))
                        for k in range(args.procs)]
                jobs[-1] = ("random", dict(payload, seed=state["seed_base"] + args.procs))
            state["seed_base"] += args.procs + 1
            for res in p.map(worker, jobs):
                pool.extend(res)
                reservoir_add(reservoir, res_seen, res, _rr)
            done += args.batch * args.procs
            state["evaluated"] += args.batch * args.procs
            pool.sort(key=lambda x: -x[0])
            pool = pool[:300]
            if cross_alt is not None and gen % 5 == 0 and len(pool) > 8:
                kept = []
                for entry in pool:
                    key = json.dumps(entry[1], sort_keys=True, default=float)
                    ok = cross_ok.get(key)
                    if ok is None:
                        try:
                            hm = ev(entry[1], cross_alt)
                        except Exception:
                            hm = None
                        ok = bool(hm and not hm["liq"])
                        cross_ok[key] = ok
                    if ok:
                        kept.append(entry)
                if kept:   # never wipe the pool entirely — keep breeding material
                    evicted = len(pool) - len(kept)
                    pool = kept
                    if evicted:
                        print(f"[{label}] cross-prune: evicted {evicted} "
                              f"opposite-fold failures", flush=True)
            json.dump(dict(pool=pool, evaluated=state["evaluated"],
                           seed_base=state["seed_base"],
                           reservoir=reservoir, res_seen=res_seen[0]),
                      open(pool_file, "w"), default=float)
            write_prog(prog_base, share, done / max(ev_target, 1) if total_budget
                       else (time.time() - t0_session) / (hours_budget * 3600) - prog_base,
                       label)
            best = f"best {pool[0][0]:.4f}" if pool else "no feasible yet"
            print(f"[{label}] gen {gen} | evaluated {done} | feasible {len(pool)} | {best}",
                  flush=True)
        return pool, reservoir

    def cross_score(pool, alt_opposite, label):
        surv = []
        for rank, (s, c, m) in enumerate(pool):
            try:
                hm = ev(c, alt_opposite)
            except Exception:
                hm = None
            if hm and not hm["liq"] and not (args.max_hold_days and
                                             hm.get("max_hold_days", 0) > args.max_hold_days):
                surv.append(dict(rank=rank + 1, cand=c, cross=hm))
        surv.sort(key=lambda x: x["cross"]["growth"], reverse=True)
        print(f"[{label}] cross-scored {len(pool)} candidates -> {len(surv)} survive "
              f"the opposite fold", flush=True)
        return surv

    altA = dict(days=fold, part="train", ranges=ranges)     # even blocks
    altB = dict(days=fold, part="holdout", ranges=ranges)   # odd blocks
    alt_full = dict(ranges=ranges) if ranges else None      # whole search region

    def merge_unique(pool, reservoir):
        seen = {json.dumps(c, sort_keys=True, default=float) for _, c, _ in pool}
        return pool + [e for e in reservoir
                       if json.dumps(e[1], sort_keys=True, default=float) not in seen]

    with mp.Pool(args.procs) as p:
        def anchor_seed(alt):
            if anchor_cand is None:
                return None
            try:
                m = ev(anchor_cand, alt)
                if m:
                    return [(m["score"], anchor_cand,
                             {k: v for k, v in m.items() if k != "trades"})] * 3
            except Exception:
                pass
            return None

        poolA, resA = search_phase(p, "poolA.json", altA, 0.38, 0.0,
                                   "fold A (even blocks)", cross_alt=altB,
                                   seed_pool=anchor_seed(altA))
        poolB, resB = ([], []) if stopped() else \
            search_phase(p, "poolB.json", altB, 0.38, 0.38,
                         "fold B (odd blocks)", cross_alt=altA,
                         seed_pool=anchor_seed(altB))

        survA = cross_score(merge_unique(poolA, resA), altB, "A->B")
        survB = cross_score(merge_unique(poolB, resB), altA, "B->A")
        write_prog(0.80, 0.0, 0.0, "cross-scoring")

        merged = []
        parents = [x["cand"] for x in survA[:12]] + [x["cand"] for x in survB[:12]]
        if len(parents) >= 2 and not stopped():
            seed_pool = []
            for c in parents:
                m = ev(c, alt_full)
                if m and feas(m, c):
                    seed_pool.append((m["score"], c, m))
            seed_pool.sort(key=lambda x: -x[0])
            merged, _ = search_phase(p, "pool_merge.json", alt_full, 0.15, 0.80,
                                     "merge (breeding survivors on both folds)",
                                     seed_pool=seed_pool)
        elif len(parents) < 2:
            print("not enough cross-fold survivors to breed — skipping merge phase",
                  flush=True)

    # ---- finalists & lockbox verdicts
    finalists = []
    seen = set()
    def add_finalist(c, origin, cross):
        key = json.dumps(c, sort_keys=True, default=float)
        if key in seen:
            return
        seen.add(key)
        finalists.append(dict(cand=c, origin=origin, cross=cross))
    for x in survA[:5]:
        add_finalist(x["cand"], f"fold A rank #{x['rank']}", x["cross"])
    for x in survB[:5]:
        add_finalist(x["cand"], f"fold B rank #{x['rank']}", x["cross"])
    for s, c, m in merged[:5]:
        add_finalist(c, "merge", None)

    for f in finalists:
        f["lockboxes"] = []
        for lb in lockboxes:
            try:
                v = ev(f["cand"], dict(ranges=[lb]))
            except Exception:
                v = None
            f["lockboxes"].append(dict(range=list(lb), verdict=v))

    def lb_key(f):
        vs = [x["verdict"] for x in f["lockboxes"]]
        g = f["cross"]["growth"] if f["cross"] else -1e9
        if lockboxes:
            if any(v is None or v["liq"] for v in vs):
                return (-1, g)              # all-fail case: least-bad by cross-fold
            return (1, min(v["growth"] for v in vs))
        return (0, g)
    winner = max(finalists, key=lb_key) if finalists else None

    total_eval = state["evaluated"]
    report = dict(fold_days=fold, lockboxes=[list(x) for x in lockboxes],
                  foldA=dict(feasible=len(poolA), survivors=len(survA)),
                  foldB=dict(feasible=len(poolB), survivors=len(survB)),
                  merged=len(merged),
                  finalists=[dict(origin=f["origin"],
                                  cross=({k: v for k, v in f["cross"].items() if k != "trades"}
                                         if f["cross"] else None),
                                  lockboxes=[dict(range=x["range"],
                                                  liq=(x["verdict"] or {}).get("liq"),
                                                  growth=(x["verdict"] or {}).get("growth"),
                                                  maxdd=(x["verdict"] or {}).get("maxdd"))
                                             for x in f["lockboxes"]])
                             for f in finalists])
    json.dump(report, open("crossfit_report.json", "w"), indent=1, default=float)

    if winner is None:
        print("\nCROSS-FIT: no candidate survived its opposite fold — nothing robust "
              "exists under these settings. No config produced.", flush=True)
        json.dump(dict(pool=[], evaluated=total_eval, seed_base=state["seed_base"]),
                  open("pool2.json", "w"))
        json.dump(dict(strategy=args.strategy, mode=args.mode, method=args.method,
                       algo="crossfit", per_regime=per_regime,
                       holdout_days=fold, lockbox=args.lockbox,
                       max_dd=args.max_dd, max_hold_days=args.max_hold_days,
                       gap_mode=args.gap_mode, scoring=args.scoring,
               anchor=args.anchor, anchor_strength=args.anchor_strength, evaluated=total_eval,
                       crossfit=report, cand=None, metrics=None,
                       generated=time.strftime("%Y-%m-%d %H:%M")),
                  open("best_config.json", "w"), indent=1, default=float)
        return None

    wc = winner["cand"]
    wm = ev(wc, alt_full) or ev(wc, None)
    # the honest single number for the table: worst lockbox verdict, else cross-fold
    verdicts = [x["verdict"] for x in winner["lockboxes"] if x["verdict"]]
    table_holdout = (min(verdicts, key=lambda v: v["growth"]) if verdicts
                     else winner["cross"])
    pool_out = [[wm["score"] if wm else 0.0, wc,
                 {k: v for k, v in (wm or {}).items() if k != "trades"}]]
    json.dump(dict(pool=pool_out, evaluated=total_eval, seed_base=state["seed_base"]),
              open("pool2.json", "w"), default=float)
    out = dict(cand=wc, metrics={k: v for k, v in (wm or {}).items() if k != "trades"},
               strategy=args.strategy, mode=args.mode, method=args.method,
               algo="crossfit", per_regime=per_regime,
               train_end=None, holdout_days=fold, lockbox=args.lockbox,
               max_dd=args.max_dd, max_hold_days=args.max_hold_days,
               gap_mode=args.gap_mode, scoring=args.scoring,
               anchor=args.anchor, anchor_strength=args.anchor_strength, evaluated=total_eval,
               holdout=({k: v for k, v in table_holdout.items() if k != "trades"}
                        if table_holdout else None),
               holdout_scan=dict(scanned=len(poolA) + len(poolB),
                                 pool=len(poolA) + len(poolB),
                                 survivors=len(survA) + len(survB)),
               crossfit=report, winner_origin=winner["origin"],
               generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open("best_config.json", "w"), indent=1, default=float)
    print(f"\nCROSS-FIT WINNER ({winner['origin']}): "
          + (f"lockbox worst {(pow(2.718281828, table_holdout['growth'])-1)*100:+.1f}%/mo"
         if verdicts else
         (f"cross-fold {(pow(2.718281828, table_holdout['growth'])-1)*100:+.1f}%/mo"
          if table_holdout else "no verdict")), flush=True)
    print(f"survivors: fold A {len(survA)}, fold B {len(survB)} | merged pool {len(merged)}",
          flush=True)
    return out


def auto_backtest(args, run_dir):
    """Publish full backtests of the run's configs and flag the run."""
    import subprocess, sys as _sys
    bt = os.path.join(B.OPT_DIR, "backtest_cli.py")
    jobs_bt = [("best_config.json", f"{args.name}_full")]
    if os.path.exists("holdout_best_config.json"):
        jobs_bt.append(("holdout_best_config.json", f"{args.name}_oosbest_full"))
    for cfg_file, bt_name in jobs_bt:
        print(f"\nauto-backtest: {cfg_file} -> '{bt_name}' ...", flush=True)
        try:
            subprocess.run([_sys.executable, bt,
                            "--config", os.path.join(run_dir, cfg_file),
                            "--name", bt_name, "--gap-mode", args.gap_mode],
                           cwd=B.OPT_DIR, check=True)
        except Exception as e:
            print(f"auto-backtest failed: {e}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="v7",
                    choices=["v7", "prime7", "v6", "scalpx", "scalpx2", "prime"])
    ap.add_argument("--algo", default="genetic",
                    choices=["random", "genetic", "refine", "crossfit"])
    ap.add_argument("--mode", required=True, choices=["lev", "spot"])
    ap.add_argument("--method", default="vol3",
                    choices=["none", "vol3", "vol3_7d", "volume3", "trend3", "volXtrend9"])
    ap.add_argument("--single-set", action="store_true",
                    help="same parameters in every regime (default: per-regime specialists)")
    ap.add_argument("--procs", type=int, default=max(1, mp.cpu_count() - 1))
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--total", type=int, default=None)
    ap.add_argument("--batch", type=int, default=120)
    ap.add_argument("--train-end", default=None, help="hold out data after this date")
    ap.add_argument("--lockbox", default=None,
                    help="cross-fit lockboxes: comma-separated date ranges 'a..b' with "
                         "open sides allowed, e.g. '2025-09-01..' or "
                         "'..2024-11-01,2025-07-01..'. Judged once, at the end.")
    ap.add_argument("--holdout-days", type=float, default=None,
                    help="alternating-block holdout: train/skip in blocks of N days "
                         "(overrides --train-end)")
    ap.add_argument("--anchor", default=None, choices=["defaults", "file"],
                    help="anchored search: seed the population from the strategy's "
                         "stored live defaults ('defaults') or from anchor_cand.json "
                         "in the run dir ('file' — written by the site when you pick "
                         "a backtest as anchor)")
    ap.add_argument("--anchor-strength", type=float, default=0.0,
                    help="0 = seeding only; >0 additionally penalizes a candidate's "
                         "score by strength x normalized parameter distance from the anchor")
    ap.add_argument("--scoring", default="classic",
                    choices=["classic", "worst_window", "underwater"],
                    help="classic (default, unchanged): mean monthly growth minus "
                         "volatility penalty. worst_window: rank by the WORST rolling "
                         "3-month stretch. underwater: classic minus a penalty for the "
                         "fraction of time capital sits locked in open positions.")
    ap.add_argument("--gap-mode", default="skip_contaminated",
                    choices=["skip_open", "skip_contaminated"],
                    help="evaluation gap handling — matches the backtest default so "
                         "candidates are searched and displayed under the same rules")
    ap.add_argument("--max-hold-days", type=float, default=None,
                    help="throw out any candidate whose simulation ever holds a "
                         "position longer than this many days (blank = unlimited)")
    ap.add_argument("--max-dd", type=float, default=None,
                    help="max drawdown cap as a fraction (default 0.80 lev / 0.50 spot)")
    ap.add_argument("--space", default=os.path.join(B.OPT_DIR, "param_space.json"))
    ap.add_argument("--resume-from", default=None, help="seed pool from another run dir")
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    if args.hours is None and args.total is None:
        args.hours = 1.0

    if args.holdout_days or args.algo == "crossfit":
        if args.train_end:
            print("note: alternating blocks override --train-end", flush=True)
        args.train_end = None
        if args.algo == "crossfit" and not args.holdout_days:
            args.holdout_days = 21.0   # default fold size
    run_dir = B.enter_run_dir(args.name)
    print(f"run dir: {run_dir} | algo: {args.algo} | procs: {args.procs}", flush=True)
    space = json.load(open(args.space)).get(args.strategy) or {}
    flat = args.strategy not in ("v7", "prime7")

    if flat:
        # shared precompute caches (instead of per-run copies)
        os.environ.setdefault("WF2_CACHE_DIR", os.path.join(B.OPT_DIR, "cache"))
        import wf2 as W
        G = W.load_globals(("v6",) if args.strategy == "prime" else (args.strategy,))
        R = G["nreg"][args.method]
        if args.single_set:
            print("note: --single-set applies to V7 only; "
                  f"{args.strategy} always searches per-regime values", flush=True)
        def eval_any(cand, t0, t1, part="train"):
            alt = (args.holdout_days, part) if args.holdout_days else None
            return W.eval_config(cand, args.method, args.mode, t0, t1, alt=alt,
                                 gap_mode=args.gap_mode, scoring=_scoring(args))
    else:
        import optimizer2 as O
        O.load_g3()
        R = O.load_g3()["regimes"][args.method][1]
        def eval_any(cand, t0, t1, part="train"):
            alt = (args.holdout_days, part) if args.holdout_days else None
            return O.eval3(cand, args.method, t0, t1, alt=alt, gap_mode=args.gap_mode,
                           scoring=_scoring(args))

    per_regime = not args.single_set
    anchor_cand = None
    if args.anchor == "file":
        anchor_cand = json.load(open("anchor_cand.json"))
        anchor_cand["mode"] = args.mode
        print(f"anchored to backtest config (strength {args.anchor_strength:g})", flush=True)
    elif args.anchor == "defaults":
        anchor_cand = build_anchor_defaults(args.strategy, args.mode, R, space)
        print(f"anchored to {args.strategy} stored live defaults "
              f"(strength {args.anchor_strength:g})", flush=True)
    if args.algo == "crossfit":
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        res = run_crossfit(args, space, R, per_regime, flat, anchor_cand=anchor_cand)
        if os.path.exists("stop.flag"):
            try: os.remove("stop.flag")
            except OSError: pass
        if res is not None:
            auto_backtest(args, run_dir)
        return

    # seed candidate from a backtest ("Optimize this" flow)
    if os.path.exists("seed_cand.json"):
        try:
            seed = json.load(open("seed_cand.json"))
            seed["mode"] = args.mode
            m = eval_any(seed, None, args.train_end)
            if m is not None and not m["liq"]:
                print(f"seeded from backtest: score {m['score']:.4f} eq {m['eq']:.0f} "
                      f"dd {m['maxdd']:.2f}", flush=True)
            else:
                print("seed evaluated but infeasible on this train window; "
                      "keeping it in the pool anyway as a breeding parent", flush=True)
            if m is not None:
                # inject strongly: several copies so genetic breeding picks it up
                if os.path.exists("pool2.json"):
                    d0 = json.load(open("pool2.json"))
                else:
                    d0 = dict(pool=[], evaluated=0, seed_base=0)
                d0["pool"] = [[m["score"], seed, m]] * 6 + d0["pool"]
                json.dump(d0, open("pool2.json", "w"), default=float)
            os.rename("seed_cand.json", "seed_cand.used.json")
        except Exception as e:
            print("seed load failed:", e, flush=True)

    pool, evaluated, seed_base, runtime_s = [], 0, 0, 0.0
    reservoir, res_seen = [], [0]
    _res_rng = np.random.default_rng(12345)
    if os.path.exists("pool2.json"):
        d = json.load(open("pool2.json"))
        pool, evaluated, seed_base = d["pool"], d["evaluated"], d["seed_base"]
        runtime_s = d.get("runtime_s", 0.0)
        reservoir = d.get("reservoir", [])
        res_seen = [d.get("res_seen", len(reservoir))]
        print(f"resuming: {len(pool)} feasible / {evaluated} evaluated", flush=True)
    if args.resume_from:
        src = os.path.join(B.OPT_DIR, args.resume_from, "pool2.json") \
            if not os.path.isabs(args.resume_from) else os.path.join(args.resume_from, "pool2.json")
        if os.path.exists(src):
            pool.extend(json.load(open(src))["pool"])
            print(f"seeded {len(pool)} candidates from {args.resume_from}", flush=True)

    if anchor_cand is not None:
        try:
            am = eval_any(anchor_cand, None, args.train_end)
            if am is not None and not am.get("liq"):
                pool = [[am["score"], anchor_cand,
                         {k: v for k, v in am.items() if k != "trades"}]] * 3 + pool
                print(f"anchor evaluated: score {am['score']:.4f} eq {am['eq']:.0f}",
                      flush=True)
            else:
                print("anchor is infeasible on this train window "
                      f"({'liquidated' if am else 'no trades'}) — used only as the "
                      "starting point for exploration, not kept in the pool", flush=True)
        except Exception as e:
            print(f"anchor evaluation failed: {e}", flush=True)

    t_end = time.time() + (args.hours * 3600 if args.hours else 10**12)
    target = evaluated + (args.total or 10**12)
    gen = 0
    t_session = time.time()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    evals_at_start = evaluated

    def write_progress():
        el = time.time() - t_session
        if args.total:
            done = evaluated - evals_at_start
            pct = min(1.0, done / max(args.total, 1))
            rate = done / max(el, 1e-9)
            eta = (args.total - done) / max(rate, 1e-9)
        else:
            pct = min(1.0, el / (args.hours * 3600))
            eta = max(0.0, args.hours * 3600 - el)
        json.dump(dict(pct=pct, eta_s=eta, elapsed_s=el,
                       evaluated_session=evaluated - evals_at_start,
                       budget=(args.total or args.hours),
                       budget_type=("evaluations" if args.total else "hours"),
                       updated=time.time()),
                  open("progress.json", "w"))
    with mp.Pool(args.procs) as p:
        while (time.time() < t_end and evaluated < target
               and not STOP["flag"] and not os.path.exists("stop.flag")):
            gen += 1
            payload = dict(space=space, R=R, mode=args.mode, method=args.method,
                           n=args.batch, t0=None, t1=args.train_end,
                           per_regime=per_regime, strategy=args.strategy,
                           max_dd=args.max_dd, max_hold=args.max_hold_days,
                           gap_mode=args.gap_mode, scoring=_scoring(args),
                           anchor_cand=anchor_cand, anchor_strength=args.anchor_strength,
                           alt=((args.holdout_days, "train") if args.holdout_days else None))
            if anchor_cand is not None and len(pool) < 24 and args.algo != "random":
                # anchored start: explore around the anchor instead of uniform randomness
                jobs = [("refine", dict(payload, seed_cand=anchor_cand, seed=seed_base + k))
                        for k in range(args.procs)]
                jobs[-1] = ("random", dict(payload, seed=seed_base + args.procs))
            elif args.algo == "random" or len(pool) < 8:
                jobs = [("random", dict(payload, seed=seed_base + k)) for k in range(args.procs)]
            elif args.algo == "genetic":
                parents = [c for _, c, _ in pool[:24]]
                jobs = [("offspring", dict(payload, parents=parents, seed=seed_base + k))
                        for k in range(args.procs)]
                # keep 1 worker exploring randomly to avoid inbreeding
                jobs[-1] = ("random", dict(payload, seed=seed_base + args.procs))
            else:  # refine
                jobs = [("refine", dict(payload, seed_cand=pool[min(k, len(pool) - 1)][1],
                                        seed=seed_base + k)) for k in range(args.procs)]
            seed_base += args.procs + 1
            for res in p.map(worker, jobs):
                pool.extend(res)
                reservoir_add(reservoir, res_seen, res, _res_rng)
            evaluated += args.batch * args.procs
            pool.sort(key=lambda x: -x[0])
            pool = pool[:300]
            json.dump(dict(pool=pool, evaluated=evaluated, seed_base=seed_base,
                           reservoir=reservoir, res_seen=res_seen[0],
                           runtime_s=runtime_s + (time.time() - t_session)),
                      open("pool2.json", "w"), default=float)
            write_progress()
            if pool:
                b = pool[0][2]
                print(f"gen {gen} | evaluated {evaluated} | feasible {len(pool)} | "
                      f"best score {pool[0][0]:.4f} eq {b['eq']:.0f} dd {b['maxdd']:.2f} "
                      f"tpm {b['tpm']:.1f}", flush=True)
            else:
                print(f"gen {gen} | evaluated {evaluated} | no feasible yet", flush=True)

    if os.path.exists("stop.flag"):
        try: os.remove("stop.flag")
        except OSError: pass
    if STOP["flag"]:
        print("stopped early — finalizing with the pool found so far.", flush=True)
    if not pool:
        print("No feasible candidates. Loosen ranges/constraints or run longer.")
        return
    best_cand, best_m = pool[0][1], pool[0][2]
    out = dict(cand=best_cand, metrics=best_m, strategy=args.strategy, mode=args.mode,
               method=args.method, algo=args.algo, per_regime=per_regime,
               train_end=args.train_end, holdout_days=args.holdout_days,
               max_dd=args.max_dd, max_hold_days=args.max_hold_days,
               gap_mode=args.gap_mode, scoring=args.scoring,
               anchor=args.anchor, anchor_strength=args.anchor_strength, evaluated=evaluated,
               generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open("best_config.json", "w"), indent=1, default=float)
    print("\nBEST -> runs/%s/best_config.json" % args.name)
    print(json.dumps(best_m, indent=1, default=float))
    if args.train_end or args.holdout_days:
        # Evaluate holdout for the TOP-10 train candidates, not just the winner.
        # The train-best is often overfit; a slightly lower-scoring candidate
        # frequently generalizes far better.
        print("\nHOLDOUT (%s) for top candidates:" %
              (f"alternating {args.holdout_days:g}-day blocks the search never saw"
               if args.holdout_days else "unseen data after %s" % args.train_end))
        holdouts = []
        for rank, (s, c, m) in enumerate(pool[:10]):
            try:
                hm = eval_any(c, args.train_end, None, part="holdout")
            except Exception:
                hm = None
            holdouts.append(hm)
            if hm:
                tag = "LIQUIDATED" if hm["liq"] else f"{(pow(2.718281828, hm['growth'])-1)*100:+.1f}%/mo dd {hm['maxdd']:.0%}"
                print(f"  #{rank+1}: train score {s:.3f} -> holdout {tag}", flush=True)
        if holdouts and holdouts[0]:
            out["holdout"] = holdouts[0]
        # walk the REST of the kept pool (train-score order) until enough
        # holdout survivors are found — with huge runs the top-10 is often
        # saturated by overfits while survivors sit deeper in the pool
        seen_keys = {json.dumps(c, sort_keys=True, default=float) for _, c, _ in pool}
        extra = [e for e in reservoir
                 if json.dumps(e[1], sort_keys=True, default=float) not in seen_keys]
        scan_src = list(pool) + extra   # elite first, then the uniform reservoir
        scan = list(holdouts)
        surv_n = sum(1 for h in scan if h and not h["liq"])
        TARGET_SURV = 10
        while len(scan) < len(scan_src) and surv_n < TARGET_SURV:
            try:
                hm = eval_any(scan_src[len(scan)][1], args.train_end, None, part="holdout")
            except Exception:
                hm = None
            scan.append(hm)
            if hm and not hm["liq"]:
                surv_n += 1
        holdouts = scan
        pool_scan = scan_src   # ranking below refers into this combined list
        survivors = [dict(rank=i + 1, train_score=pool_scan[i][0], holdout=holdouts[i])
                     for i in range(len(holdouts))
                     if holdouts[i] and not holdouts[i]["liq"]
                     and not (args.max_hold_days and
                              holdouts[i].get("max_hold_days", 0) > args.max_hold_days)]
        survivors.sort(key=lambda r: r["holdout"]["growth"], reverse=True)
        out["holdout_scan"] = dict(scanned=len(scan), pool=len(pool_scan),
                                   reservoir=len(extra), survivors=len(survivors))
        out["holdout_survivors"] = survivors[:10]
        print(f"\nPOOL SCAN: evaluated holdout for {len(scan)}/{len(pool)} kept candidates; "
              f"{len(survivors)} survived", flush=True)
        for s in survivors[:5]:
            hm = s["holdout"]
            print(f"  train-rank #{s['rank']}: {(pow(2.718281828, hm['growth'])-1)*100:+.1f}%/mo "
                  f"dd {hm['maxdd']:.0%}", flush=True)
        # survivors-first ranking: non-liquidated by holdout growth, then the rest
        ranked = [dict(rank=i + 1, train_score=pool[i][0], holdout=holdouts[i])
                  for i in range(len(holdouts))]
        ranked.sort(key=lambda r: -1e9 if (r["holdout"] is None or r["holdout"]["liq"])
                    else r["holdout"]["growth"], reverse=True)
        out["holdout_top10"] = ranked
        surv = [r for r in ranked if r["holdout"] and not r["holdout"]["liq"]]
        print(f"\nSURVIVORS-FIRST: {len(surv)}/{len(ranked)} candidates survived the holdout")
        for r in ranked[:5]:
            hm = r["holdout"]
            tag = "no data" if hm is None else ("LIQUIDATED" if hm["liq"] else
                  f"{(pow(2.718281828, hm['growth'])-1)*100:+.1f}%/mo dd {hm['maxdd']:.0%}")
            print(f"  train-rank #{r['rank']}: {tag}")
        # pick the best genuine OOS performer among the top-10
        def hkey(hm):
            if hm is None or hm["liq"]:
                return -1e9
            return hm["growth"]
        best_h = max(range(len(holdouts)), key=lambda i: hkey(holdouts[i]))
        if holdouts[best_h] and hkey(holdouts[best_h]) > -1e9 and best_h != 0:
            s2, c2, m2 = pool[best_h]
            hb = dict(cand=c2, metrics=m2, holdout=holdouts[best_h],
                      strategy=args.strategy, mode=args.mode, method=args.method,
                      note=f"OOS-best from pool rank #{best_h+1} (train-best was rank #1). "
                           "Caveat: picked USING the holdout, so re-verify with walk-forward before trusting.")
            json.dump(hb, open("holdout_best_config.json", "w"), indent=1, default=float)
            out["holdout_best"] = dict(rank=best_h + 1, holdout=holdouts[best_h])
            print(f"\nOOS-BEST is pool rank #{best_h+1} -> saved to holdout_best_config.json")
        # seed comparison, if this run was seeded from a backtest
        if os.path.exists("seed_cand.used.json"):
            try:
                seed = json.load(open("seed_cand.used.json"))
                seed["mode"] = args.mode
                sh = eval_any(seed, args.train_end, None, part="holdout")
                if sh:
                    tag = "LIQUIDATED" if sh["liq"] else f"{(pow(2.718281828, sh['growth'])-1)*100:+.1f}%/mo"
                    print(f"\nSEED holdout for comparison: {tag}")
                    out["seed_holdout"] = sh
            except Exception:
                pass
        json.dump(out, open("best_config.json", "w"), indent=1, default=float)

    auto_backtest(args, run_dir)


if __name__ == "__main__":
    main()
