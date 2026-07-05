#!/usr/bin/env python3
"""Final full-data production search for a (strategy, mode, method) family.
Resumable across invocations via a feasible-pool JSON."""
import json, os, sys, time
import numpy as np
import multiprocessing as mp
from wf2 import (load_globals, sample_v6, sample_scalpx, eval_config,
                 feasible, average_cands)

def run(strategy, mode, method, n_samples, seed):
    G = load_globals((strategy,))
    R = G["nreg"][method]
    rng = np.random.default_rng(seed)
    sampler = sample_v6 if strategy == "v6" else sample_scalpx
    out = []
    for _ in range(n_samples):
        c = sampler(rng, R, mode)
        m = eval_config(c, method, mode, None, "2026-07-02")
        if feasible(m, mode):
            out.append((m["score"], c, {k: v for k, v in m.items() if k != "trades"}))
    return out

def worker(args):
    return run(*args)

if __name__ == "__main__":
    strategy, mode, method = sys.argv[1], sys.argv[2], sys.argv[3]
    total = int(sys.argv[4]) if len(sys.argv) > 4 else 1200
    pool_file = f"final_pool_{strategy}_{mode}_{method}.json"
    existing = []
    seed_base = 0
    if os.path.exists(pool_file):
        d = json.load(open(pool_file))
        existing = d["pool"]; seed_base = d["seed_base"]
    load_globals((strategy,))
    per = max(1, total // 4)
    with mp.Pool(4) as p:
        res = p.map(worker, [(strategy, mode, method, per, seed_base + k) for k in range(4)])
    for r in res:
        existing.extend([(s, c, m) for s, c, m in r])
    json.dump(dict(pool=existing, seed_base=seed_base + 4), open(pool_file, "w"), default=float)
    existing.sort(key=lambda x: -x[0])
    print(f"pool size {len(existing)}")
    if existing:
        top = [c for _, c, _ in existing[:5]]
        avg = average_cands(top)
        m = eval_config(avg, method, mode, None, "2026-07-02")
        chosen, cm = (avg, m) if feasible(m, mode) else (existing[0][1], existing[0][2])
        json.dump(dict(cand=chosen, metrics={k: v for k, v in cm.items() if k != "trades"},
                       method=method, mode=mode, strategy=strategy),
                  open(f"final_config_{strategy}_{mode}_{method}.json", "w"),
                  indent=1, default=float)
        print("chosen:", json.dumps({k: v for k, v in cm.items() if k != "trades"}, default=float))
