#!/usr/bin/env python3
"""Remove invalid msw_*/m1w_* run dirs from optimizer/runs.
A dir is invalid if its name already sits in the quarantine dir (duplicate of
a known-bad run) OR its newest file predates the fixed-plan restart cutoff.
Valid post-restart work (new mtimes, not in quarantine) is left alone.
Usage: python3 clean_sweep_dupes.py <runs_dir> <quarantine_dir> [cutoff_epoch]
"""
import os, sys, glob, shutil, time

runs = sys.argv[1]
quar = sys.argv[2]
cutoff = float(sys.argv[3]) if len(sys.argv) > 3 else time.mktime(
    time.strptime("2026-08-10 21:20:00", "%Y-%m-%d %H:%M:%S"))
os.makedirs(quar, exist_ok=True)
dupes = moved = kept = 0
for d in glob.glob(os.path.join(runs, "msw_*")) + glob.glob(os.path.join(runs, "m1w_*")):
    name = os.path.basename(d)
    newest = max((os.path.getmtime(os.path.join(r, f))
                  for r, _, fs in os.walk(d) for f in fs), default=0)
    if os.path.exists(os.path.join(quar, name)):
        shutil.rmtree(d); dupes += 1
    elif newest and newest < cutoff:
        shutil.move(d, quar); moved += 1
    else:
        kept += 1
print(f"dupes-removed={dupes} quarantined={moved} kept-valid={kept}")
