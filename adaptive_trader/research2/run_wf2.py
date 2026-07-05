#!/usr/bin/env python3
"""Parallel walk-forward runner. Resumable: one JSON per (config, fold).
Run in background:  nohup python3 run_wf2.py --stage A &
"""
import argparse, json, os, sys, time
import multiprocessing as mp
import pandas as pd
import numpy as np

import wf2
from wf2 import run_fold, REFIT_DATES, load_globals
from regimes import REGIME_METHODS

RESULT_DIR = "wf2_results"

def config_id(strategy, mode, method, window):
    return f"{strategy}__{mode}__{method}__{window}"

def jobs_stage_A(n_samples=500):
    """Method screening: expanding window, all regime methods, both strategies/modes."""
    jobs = []
    for strategy in ["v6", "scalpx"]:
        for mode in ["lev", "spot"]:
            for method in REGIME_METHODS:
                for k in range(len(REFIT_DATES)):
                    jobs.append(dict(strategy=strategy, mode=mode, method=method,
                                     window="all", fold_idx=k, n_samples=n_samples,
                                     seed=hash((strategy, mode, method)) % 10**6))
    return jobs

def jobs_stage_B(winners, n_samples=700):
    """Window study on winning methods."""
    jobs = []
    for (strategy, mode, method) in winners:
        for window in [42, 91, 182]:
            for k in range(len(REFIT_DATES)):
                jobs.append(dict(strategy=strategy, mode=mode, method=method,
                                 window=window, fold_idx=k, n_samples=n_samples,
                                 seed=hash((strategy, mode, method, window)) % 10**6))
    return jobs

def job_path(j):
    cid = config_id(j["strategy"], j["mode"], j["method"], j["window"])
    return os.path.join(RESULT_DIR, cid, f"fold_{j['fold_idx']:02d}.json")

def worker(j):
    path = job_path(j)
    if os.path.exists(path):
        return path
    t0 = time.time()
    try:
        r = run_fold(j)
    except Exception as e:
        r = dict(job=j, status="error", error=str(e))
    r["elapsed"] = time.time() - t0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(r, f, default=float)
    return path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="A")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--samples", type=int, default=350)
    ap.add_argument("--only", default=None,
                    help="config id filter substring, e.g. v6__lev__vol3__all")
    ap.add_argument("--max-jobs", type=int, default=10**9)
    args = ap.parse_args()

    if args.stage == "A":
        jobs = jobs_stage_A(args.samples)
    else:
        winners = json.load(open("wf2_winners.json"))
        jobs = jobs_stage_B([tuple(w) for w in winners], args.samples)
    if args.only:
        jobs = [j for j in jobs if args.only in config_id(
            j["strategy"], j["mode"], j["method"], j["window"])]
    jobs = [j for j in jobs if not os.path.exists(job_path(j))]
    jobs = jobs[: args.max_jobs]
    if not jobs:
        print("ALL_DONE")
        return
    strategies = {j["strategy"] for j in jobs}
    load_globals(tuple(strategies))  # load once; workers fork-inherit
    print(f"{len(jobs)} jobs to run", flush=True)
    with mp.Pool(args.procs) as pool:
        for i, path in enumerate(pool.imap_unordered(worker, jobs)):
            print(f"[{i+1}/{len(jobs)}] {os.path.basename(os.path.dirname(path))}/{os.path.basename(path)}", flush=True)
    print("BATCH_DONE")

if __name__ == "__main__":
    main()
