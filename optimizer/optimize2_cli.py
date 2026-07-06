#!/usr/bin/env python3
"""Optimizer v2 CLI — full parameter search (thresholds + indicator lengths)
on the V7 engine, with algorithm choice and per-regime specialist sets.

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
import argparse, json, os, time
import multiprocessing as mp
import numpy as np


def worker(args):
    kind, payload = args
    import optimizer2 as O
    O.load_g3()
    rng = np.random.default_rng(payload["seed"])
    space = payload["space"]
    if kind == "random":
        return O.batch_random(rng, space, payload["R"], payload["mode"],
                              payload["method"], payload["n"],
                              payload["t0"], payload["t1"], payload["per_regime"])
    if kind == "offspring":
        return O.batch_offspring(rng, space, payload["mode"], payload["method"],
                                 payload["parents"], payload["n"],
                                 payload["t0"], payload["t1"])
    if kind == "refine":
        return O.batch_refine(rng, space, payload["mode"], payload["method"],
                              payload["seed_cand"], payload["n"],
                              payload["t0"], payload["t1"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
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
    ap.add_argument("--space", default=os.path.join(B.OPT_DIR, "param_space.json"))
    ap.add_argument("--resume-from", default=None, help="seed pool from another run dir")
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    if args.hours is None and args.total is None:
        args.hours = 1.0

    run_dir = B.enter_run_dir(args.name)
    print(f"run dir: {run_dir} | algo: {args.algo} | procs: {args.procs}", flush=True)
    space = json.load(open(args.space))["v7"]

    import optimizer2 as O
    O.load_g3()
    R = O.load_g3()["regimes"][args.method][1]

    # seed candidate from a backtest ("Optimize this" flow)
    if os.path.exists("seed_cand.json"):
        try:
            seed = json.load(open("seed_cand.json"))
            seed["mode"] = args.mode
            m = O.eval3(seed, args.method, None, args.train_end)
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
    with mp.Pool(args.procs) as p:
        while time.time() < t_end and evaluated < target:
            gen += 1
            payload = dict(space=space, R=R, mode=args.mode, method=args.method,
                           n=args.batch, t0=None, t1=args.train_end,
                           per_regime=per_regime)
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
            if pool:
                b = pool[0][2]
                print(f"gen {gen} | evaluated {evaluated} | feasible {len(pool)} | "
                      f"best score {pool[0][0]:.4f} eq {b['eq']:.0f} dd {b['maxdd']:.2f} "
                      f"tpm {b['tpm']:.1f}", flush=True)
            else:
                print(f"gen {gen} | evaluated {evaluated} | no feasible yet", flush=True)

    if not pool:
        print("No feasible candidates. Loosen ranges/constraints or run longer.")
        return
    best_cand, best_m = pool[0][1], pool[0][2]
    out = dict(cand=best_cand, metrics=best_m, strategy="v7", mode=args.mode,
               method=args.method, algo=args.algo, per_regime=per_regime,
               train_end=args.train_end, evaluated=evaluated,
               generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open("best_config.json", "w"), indent=1, default=float)
    print("\nBEST -> runs/%s/best_config.json" % args.name)
    print(json.dumps(best_m, indent=1, default=float))
    if args.train_end:
        # Evaluate holdout for the TOP-10 train candidates, not just the winner.
        # The train-best is often overfit; a slightly lower-scoring candidate
        # frequently generalizes far better.
        print("\nHOLDOUT (unseen data after %s) for top candidates:" % args.train_end)
        holdouts = []
        for rank, (s, c, m) in enumerate(pool[:10]):
            try:
                hm = O.eval3(c, args.method, args.train_end, None)
            except Exception:
                hm = None
            holdouts.append(hm)
            if hm:
                tag = "LIQUIDATED" if hm["liq"] else f"{(pow(2.718281828, hm['growth'])-1)*100:+.1f}%/mo dd {hm['maxdd']:.0%}"
                print(f"  #{rank+1}: train score {s:.3f} -> holdout {tag}", flush=True)
        if holdouts and holdouts[0]:
            out["holdout"] = holdouts[0]
        # pick the best genuine OOS performer among the top-10
        def hkey(hm):
            if hm is None or hm["liq"]:
                return -1e9
            return hm["growth"]
        best_h = max(range(len(holdouts)), key=lambda i: hkey(holdouts[i]))
        if holdouts[best_h] and hkey(holdouts[best_h]) > -1e9 and best_h != 0:
            s2, c2, m2 = pool[best_h]
            hb = dict(cand=c2, metrics=m2, holdout=holdouts[best_h],
                      strategy="v7", mode=args.mode, method=args.method,
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
                sh = O.eval3(seed, args.method, args.train_end, None)
                if sh:
                    tag = "LIQUIDATED" if sh["liq"] else f"{(pow(2.718281828, sh['growth'])-1)*100:+.1f}%/mo"
                    print(f"\nSEED holdout for comparison: {tag}")
                    out["seed_holdout"] = sh
            except Exception:
                pass
        json.dump(out, open("best_config.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
