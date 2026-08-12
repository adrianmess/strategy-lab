#!/usr/bin/env python3
"""Re-run an FCFS combo's ENTIRE evidence chain on the data currently on
disk: first re-backtest every component run (its train-best `_full` entry and
its OOS-best `_oosbest_full` entry, matching what the Backtests page shows),
then re-simulate the combo itself (overwrites `_fcfs_full` / `_fcfs_wf` and
the verdict). Run with cwd = optimizer/.

Usage: python3 ../scripts/refresh_combo.py <combo_run_name> [--max-dd 0.5]
"""
import json
import os
import subprocess
import sys

OPT = os.getcwd()                       # spawned with cwd=optimizer
RUNS = os.path.join(OPT, "runs")

name = sys.argv[1]
extra = sys.argv[2:]
b = json.load(open(os.path.join(RUNS, name, "best_config.json")))
comps = [c.get("run") for c in ((b.get("cand") or {}).get("components") or [])]
comps = [c for c in comps if c]
if len(comps) < 2:
    sys.exit(f"'{name}' has no stored component list")

print(f"=== refreshing evidence chain for {name}: "
      f"{len(comps)} components, then the combo ===", flush=True)
fail = 0
for i, r in enumerate(comps, 1):
    for cfg, sfx in (("best_config.json", "_full"),
                     ("holdout_best_config.json", "_oosbest_full")):
        p = os.path.join(RUNS, r, cfg)
        if not os.path.exists(p):
            continue
        print(f"[{i}/{len(comps)}] backtest {r}{sfx}", flush=True)
        rc = subprocess.run(
            [sys.executable, "backtest_cli.py", "--config", p,
             "--name", f"{r}{sfx}", "--gap-mode", "skip_contaminated"],
            cwd=OPT, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT).returncode
        if rc:
            fail += 1
            print(f"  !! backtest failed rc={rc} — continuing", flush=True)
print(f"=== component backtests done ({fail} failures) — "
      f"re-simulating the combo ===", flush=True)
cmd = [sys.executable, "fcfsx_cli.py", "--name", name,
       "--runs", ",".join(comps)] + extra
sys.exit(subprocess.run(cmd, cwd=OPT).returncode)
