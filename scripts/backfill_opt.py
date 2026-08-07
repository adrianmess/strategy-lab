#!/usr/bin/env python3
"""One-time: backfill the 'opt' (optimizer settings) block on backtest entries
whose publisher didn't record it — macdx/rocx/original code paths never passed
opt, and *_oosbest entries were published from holdout_best_config.json which
lacks the run-level settings. Maps each entry back to its run's
best_config.json. Safe to re-run."""
import json, os, fcntl
from prune_backtests import stamp_opt

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "..", "dashboard", "backtests.js")

with open(P + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    txt = open(P).read()
    entries = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
    del txt
    before = sum(1 for e in entries if not e.get("opt"))
    for e in entries:
        stamp_opt(e)
    after = sum(1 for e in entries if not e.get("opt"))
    tmp = P + ".tmpopt"
    with open(tmp, "w") as f:
        f.write("window.BACKTESTS=")
        json.dump(entries, f)
        f.write(";")
    os.replace(tmp, P)
print(f"entries missing opt: {before} -> {after} "
      f"(stamped {before - after} of {len(entries)})")
