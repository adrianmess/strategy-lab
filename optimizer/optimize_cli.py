#!/usr/bin/env python3
"""Long-running multi-process parameter search on full (or clipped) history.

Examples:
  # 8 processes, run for 2 hours, max-risk leveraged V6:
  python3 optimize_cli.py --strategy v6 --mode lev --method none \
      --procs 8 --hours 2 --name my_lev_run

  # spot search until 50k candidates evaluated:
  python3 optimize_cli.py --strategy v6 --mode spot --method vol3 \
      --procs 8 --total 50000 --name my_spot_run

Resumable: re-running with the same --name continues from the saved pool.
Output: runs/<name>/best_config.json (+ pool.json checkpoint).
"""
import _bootstrap as B
import argparse, json, os, time
import multiprocessing as mp
import numpy as np


def worker(args):
    strategy, mode, method, n, seed = args
    from wf2 import load_globals, sample_v6, sample_scalpx, eval_config, feasible
    load_globals((strategy,))
    import numpy as np
    rng = np.random.default_rng(seed)
    sampler = sample_v6 if strategy == "v6" else sample_scalpx
    from wf2 import load_globals as _lg
    G = _lg((strategy,))
    R = G["nreg"][method]
    out = []
    for _ in range(n):
        c = sampler(rng, R, mode)
        m = eval_config(c, method, mode, None, "2099-01-01")
        if feasible(m, mode):
            out.append((m["score"], c, {k: v for k, v in m.items() if k != "trades"}))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True, choices=["v6", "scalpx"])
    ap.add_argument("--mode", required=True, choices=["lev", "spot"])
    ap.add_argument("--method", default="none",
                    choices=["none", "vol3", "vol3_7d", "volume3", "trend3", "volXtrend9"])
    ap.add_argument("--procs", type=int, default=max(1, mp.cpu_count() - 1),
                    help="number of worker processes (default: all cores minus one)")
    ap.add_argument("--hours", type=float, default=None, help="time budget")
    ap.add_argument("--total", type=int, default=None, help="candidate budget")
    ap.add_argument("--batch", type=int, default=200, help="candidates per worker batch")
    ap.add_argument("--name", required=True, help="run name (resumable)")
    args = ap.parse_args()
    if args.hours is None and args.total is None:
        args.hours = 0.5

    run_dir = B.enter_run_dir(args.name)
    print(f"run dir: {run_dir} | procs: {args.procs}")
    pool_file = "pool.json"
    pool, seed_base, evaluated, runtime_s = [], 0, 0, 0.0
    if os.path.exists(pool_file):
        d = json.load(open(pool_file))
        pool, seed_base, evaluated = d["pool"], d["seed_base"], d.get("evaluated", 0)
        runtime_s = d.get("runtime_s", 0.0)
        print(f"resuming: {len(pool)} feasible from {evaluated} evaluated")

    from wf2 import load_globals, eval_config, feasible, average_cands
    load_globals((args.strategy,))  # build caches in parent before forking

    t_end = time.time() + (args.hours * 3600 if args.hours else 10**12)
    target = evaluated + (args.total or 10**12)
    t_session = time.time()
    with mp.Pool(args.procs) as p:
        while time.time() < t_end and evaluated < target:
            batch_jobs = [(args.strategy, args.mode, args.method, args.batch,
                           seed_base + k) for k in range(args.procs)]
            seed_base += args.procs
            for res in p.map(worker, batch_jobs):
                pool.extend(res)
            evaluated += args.batch * args.procs
            pool.sort(key=lambda x: -x[0])
            pool = pool[:200]  # keep top 200
            json.dump(dict(pool=pool, seed_base=seed_base, evaluated=evaluated,
                           runtime_s=runtime_s + (time.time() - t_session)),
                      open(pool_file, "w"), default=float)
            best = pool[0][2] if pool else None
            print(f"evaluated {evaluated} | feasible kept {len(pool)} | "
                  f"best score {pool[0][0]:.4f} eq {best['eq']:.0f}" if pool else
                  f"evaluated {evaluated} | no feasible yet", flush=True)

    if not pool:
        print("No feasible candidates found. Loosen constraints or run longer.")
        return
    top = [c for _, c, _ in pool[:5]]
    avg = average_cands(top)
    m = eval_config(avg, args.method, args.mode, None, "2099-01-01")
    chosen, cm = (avg, m) if feasible(m, args.mode) else (pool[0][1], pool[0][2])
    out = dict(cand=chosen, metrics={k: v for k, v in cm.items() if k != "trades"},
               strategy=args.strategy, mode=args.mode, method=args.method,
               evaluated=evaluated, generated=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open("best_config.json", "w"), indent=1, default=float)
    print("\nBEST CONFIG -> runs/%s/best_config.json" % args.name)
    print(json.dumps(out["metrics"], indent=1, default=float))
    print("\nNext: create a dashboard entry with:")
    print(f"  python3 backtest_cli.py --config runs/{args.name}/best_config.json --name {args.name}")


if __name__ == "__main__":
    main()
