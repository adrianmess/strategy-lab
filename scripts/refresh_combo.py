#!/usr/bin/env python3
"""Re-run an FCFS combo's ENTIRE evidence chain on the data currently on
disk: first re-backtest every component run (its train-best `_full` entry and
its OOS-best `_oosbest_full` entry, matching what the Backtests page shows),
then re-simulate the combo itself (overwrites `_fcfs_full` / `_fcfs_wf` and
the verdict). Run with cwd = optimizer/.

Usage: python3 ../scripts/refresh_combo.py <combo> [--jobs N] [--max-dd 0.5]
--jobs N runs up to N component backtests in parallel (each backtest is
single-core; the shared publish lock serializes only the final write).
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

OPT = os.getcwd()                       # spawned with cwd=optimizer
RUNS = os.path.join(OPT, "runs")

args = sys.argv[1:]
name = args.pop(0)
jobs = 1
if "--jobs" in args:
    i = args.index("--jobs")
    jobs = max(1, min(10, int(args[i + 1])))
    args = args[:i] + args[i + 2:]
extra = args
b = json.load(open(os.path.join(RUNS, name, "best_config.json")))
comps = [c.get("run") for c in ((b.get("cand") or {}).get("components") or [])]
comps = [c for c in comps if c]
if len(comps) < 2:
    sys.exit(f"'{name}' has no stored component list")

tasks = []
for r in comps:
    for cfg, sfx in (("best_config.json", "_full"),
                     ("holdout_best_config.json", "_oosbest_full")):
        p = os.path.join(RUNS, r, cfg)
        if os.path.exists(p):
            tasks.append((r, p, sfx))
print(f"=== refreshing evidence chain for {name}: {len(comps)} components "
      f"({len(tasks)} backtests, {jobs} in parallel), then the combo ===",
      flush=True)
done = [0]
def bt(t):
    r, p, sfx = t
    rc = subprocess.run(
        [sys.executable, "backtest_cli.py", "--config", p,
         "--name", f"{r}{sfx}", "--gap-mode", "skip_contaminated"],
        cwd=OPT, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT).returncode
    done[0] += 1
    print(f"[{done[0]}/{len(tasks)}] {r}{sfx}"
          + (f"  !! FAILED rc={rc}" if rc else ""), flush=True)
    return rc

with ThreadPoolExecutor(max_workers=jobs) as ex:
    fails = sum(1 for rc in ex.map(bt, tasks) if rc)
print(f"=== component backtests done ({fails} failures) — "
      f"re-simulating the combo ===", flush=True)
cmd = [sys.executable, "fcfsx_cli.py", "--name", name,
       "--runs", ",".join(comps)] + extra
sys.exit(subprocess.run(cmd, cwd=OPT).returncode)
