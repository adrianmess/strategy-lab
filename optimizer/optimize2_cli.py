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
                                 max_hold=payload["max_hold"], gap_mode=payload["gap_mode"])
        elif kind == "offspring":
            res = O.batch_offspring(rng, space, payload["mode"], payload["method"],
                                    payload["parents"], payload["n"],
                                    payload["t0"], payload["t1"],
                                    max_dd=payload["max_dd"], alt=payload["alt"],
                                    max_hold=payload["max_hold"], gap_mode=payload["gap_mode"])
        elif kind == "refine":
            res = O.batch_refine(rng, space, payload["mode"], payload["method"],
                                 payload["seed_cand"], payload["n"],
                                 payload["t0"], payload["t1"],
                                 max_dd=payload["max_dd"], alt=payload["alt"],
                                 max_hold=payload["max_hold"], gap_mode=payload["gap_mode"])
        else:
            res = []
        for _s, _c, _m in res:
            _c["strategy"] = strategy
        return res
    # flat-candidate strategies (prime / v6 / scalpx) via the wf2 engine
    import wf2 as W
    G = W.load_globals(("v6",) if strategy == "prime" else (strategy,))
    R = G["nreg"][payload["method"]]
    strip = lambda res: [(s, c, {k: v for k, v in m.items() if k != "trades"})
                         for s, c, m in res]
    if kind == "offspring":
        return strip(W.batch_offspring_flat(rng, payload["parents"], payload["mode"],
                                            space, None, R, payload["method"],
                                            payload["n"], payload["t0"], payload["t1"],
                                            max_dd=payload["max_dd"], alt=payload["alt"],
                                            max_hold=payload["max_hold"],
                                            gap_mode=payload["gap_mode"]))
    if kind == "refine":
        return strip(W.batch_refine_flat(rng, payload["seed_cand"], payload["mode"],
                                         space, payload["method"],
                                         payload["n"], payload["t0"], payload["t1"],
                                         max_dd=payload["max_dd"], alt=payload["alt"],
                                         max_hold=payload["max_hold"],
                                         gap_mode=payload["gap_mode"]))
    sampler = {"v6": W.sample_v6, "prime": W.sample_prime,
               "scalpx2": W.sample_scalpx2}.get(strategy, W.sample_scalpx)
    out = []
    for _ in range(payload["n"]):
        c = sampler(rng, R, payload["mode"], space)
        m = W.eval_config(c, payload["method"], payload["mode"],
                          payload["t0"], payload["t1"], alt=payload["alt"],
                          gap_mode=payload["gap_mode"])
        if W.feasible(m, payload["mode"], cand=c, max_dd=payload["max_dd"],
                      max_hold=payload["max_hold"]):
            out.append((m["score"], c, {k: v for k, v in m.items() if k != "trades"}))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="v7",
                    choices=["v7", "prime7", "v6", "scalpx", "scalpx2", "prime"])
    ap.add_argument("--algo", default="genetic", choices=["random", "genetic", "refine"])
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
    ap.add_argument("--holdout-days", type=float, default=None,
                    help="alternating-block holdout: train/skip in blocks of N days "
                         "(overrides --train-end)")
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

    if args.holdout_days:
        if args.train_end:
            print("note: --holdout-days overrides --train-end (alternating blocks)", flush=True)
        args.train_end = None
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
                                 gap_mode=args.gap_mode)
    else:
        import optimizer2 as O
        O.load_g3()
        R = O.load_g3()["regimes"][args.method][1]
        def eval_any(cand, t0, t1, part="train"):
            alt = (args.holdout_days, part) if args.holdout_days else None
            return O.eval3(cand, args.method, t0, t1, alt=alt, gap_mode=args.gap_mode)

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
    per_regime = not args.single_set

    pool, evaluated, seed_base, runtime_s = [], 0, 0, 0.0
    if os.path.exists("pool2.json"):
        d = json.load(open("pool2.json"))
        pool, evaluated, seed_base = d["pool"], d["evaluated"], d["seed_base"]
        runtime_s = d.get("runtime_s", 0.0)
        print(f"resuming: {len(pool)} feasible / {evaluated} evaluated", flush=True)
    if args.resume_from:
        src = os.path.join(B.OPT_DIR, args.resume_from, "pool2.json") \
            if not os.path.isabs(args.resume_from) else os.path.join(args.resume_from, "pool2.json")
        if os.path.exists(src):
            pool.extend(json.load(open(src))["pool"])
            print(f"seeded {len(pool)} candidates from {args.resume_from}", flush=True)

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
                           gap_mode=args.gap_mode,
                           alt=((args.holdout_days, "train") if args.holdout_days else None))
            if args.algo == "random" or len(pool) < 8:
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
            evaluated += args.batch * args.procs
            pool.sort(key=lambda x: -x[0])
            pool = pool[:300]
            json.dump(dict(pool=pool, evaluated=evaluated, seed_base=seed_base,
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
               gap_mode=args.gap_mode, evaluated=evaluated,
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
        scan = list(holdouts)
        surv_n = sum(1 for h in scan if h and not h["liq"])
        TARGET_SURV = 10
        while len(scan) < len(pool) and surv_n < TARGET_SURV:
            try:
                hm = eval_any(pool[len(scan)][1], args.train_end, None, part="holdout")
            except Exception:
                hm = None
            scan.append(hm)
            if hm and not hm["liq"]:
                surv_n += 1
        holdouts = scan
        survivors = [dict(rank=i + 1, train_score=pool[i][0], holdout=holdouts[i])
                     for i in range(len(holdouts))
                     if holdouts[i] and not holdouts[i]["liq"]
                     and not (args.max_hold_days and
                              holdouts[i].get("max_hold_days", 0) > args.max_hold_days)]
        survivors.sort(key=lambda r: r["holdout"]["growth"], reverse=True)
        out["holdout_scan"] = dict(scanned=len(scan), pool=len(pool),
                                   survivors=len(survivors))
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


if __name__ == "__main__":
    main()
