#!/usr/bin/env python3
"""Walk-forward study: re-optimize every N days on a rolling/expanding window,
evaluate on the next unseen window, then run the honest CONTINUOUS re-simulation.

Example (8 processes, monthly refits, expanding window):
  python3 walkforward_cli.py --strategy v6 --mode lev --method none \
      --window all --refit-days 28 --samples 500 --procs 8 --name wf_lev

Output: runs/<name>/wf_results/... fold artifacts, resim.json (continuous OOS),
       and a dashboard entry if --publish is given.
"""
import _bootstrap as B
import argparse, json, os, time
import multiprocessing as mp
import pandas as pd
import numpy as np


def worker(job):
    import wf2
    wf2.REFIT_DATES = pd.to_datetime(job.pop("_refit_dates"))
    wf2.TEST_DAYS = job.pop("_test_days")
    from wf2 import run_fold, load_globals
    load_globals((job["strategy"],))
    path = job.pop("_path")
    t0 = time.time()
    try:
        r = run_fold(job)
    except Exception as e:
        r = dict(job=job, status="error", error=str(e))
    r["elapsed"] = time.time() - t0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(r, open(path, "w"), default=float)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True, choices=["v6", "scalpx"])
    ap.add_argument("--mode", required=True, choices=["lev", "spot"])
    ap.add_argument("--method", default="none")
    ap.add_argument("--window", default="all", help="'all' or days (42/91/182/365)")
    ap.add_argument("--refit-days", type=int, default=28)
    ap.add_argument("--oos-start", default="2024-11-15")
    ap.add_argument("--samples", type=int, default=500, help="candidates per refit")
    ap.add_argument("--procs", type=int, default=max(1, mp.cpu_count() - 1))
    ap.add_argument("--name", required=True)
    args = ap.parse_args()

    run_dir = B.enter_run_dir(args.name)
    print(f"run dir: {run_dir} | procs: {args.procs}")

    import wf2
    # refit schedule: from oos-start to (data end - refit-days)
    from common import load_segments
    segs = load_segments()
    data_end = max(g["t"].max() for g, _ in segs)
    dates = pd.date_range(args.oos_start,
                          data_end - pd.Timedelta(days=args.refit_days),
                          freq=f"{args.refit_days}D")
    wf2.REFIT_DATES = dates
    wf2.TEST_DAYS = args.refit_days
    print(f"{len(dates)} refits from {dates[0].date()} to {dates[-1].date()}")

    window = args.window if args.window == "all" else int(args.window)
    cid = f"{args.strategy}__{args.mode}__{args.method}__{window}"
    jobs = []
    for k in range(len(dates)):
        path = os.path.join("wf_results", cid, f"fold_{k:02d}.json")
        if os.path.exists(path):
            continue
        jobs.append(dict(strategy=args.strategy, mode=args.mode, method=args.method,
                         window=window, fold_idx=k, n_samples=args.samples,
                         seed=abs(hash(cid)) % 10**6,
                         _path=path, _refit_dates=[str(d) for d in dates],
                         _test_days=args.refit_days))
    print(f"{len(jobs)} folds to compute")
    if jobs:
        wf2.load_globals((args.strategy,))
        with mp.Pool(args.procs) as p:
            for i, path in enumerate(p.imap_unordered(worker, jobs)):
                print(f"[{i+1}/{len(jobs)}] {path}", flush=True)

    # continuous OOS re-simulation (the honest number)
    import resim
    r = resim.resim_config(cid, base="wf_results")
    json.dump(r, open("resim.json", "w"), default=float)
    show = {k: v for k, v in r.items() if k != "curve"}
    print("\nCONTINUOUS OOS RESULT:")
    print(json.dumps(show, indent=1, default=float))
    print(f"\nDashboard entry: python3 ../../backtest_cli.py --walkforward runs/{args.name} --name {args.name}")


if __name__ == "__main__":
    main()
