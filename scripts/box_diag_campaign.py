#!/usr/bin/env python3
"""Diagnose why a box's gamut worker finds no work. Run ON the box from
~/strategy-lab/optimizer:  python3 box_diag_campaign.py <campaign>"""
import json
import os
import sys

camp = sys.argv[1] if len(sys.argv) > 1 else "spotg3m_0811"
p = json.load(open(os.path.join("campaigns", camp, "plan.json")))
specs = [s for s in p["specs"] if s.get("status") != "done"]
marker = 0
for s in specs:
    d = os.path.join("runs", s["name"])
    if os.path.exists(os.path.join(d, "best_config.json")) or \
            os.path.exists(os.path.join(d, "no_survivor.json")):
        marker += 1
sp = os.path.join("campaigns", camp, "worker_state.json")
st = json.load(open(sp)) if os.path.exists(sp) else {}
stop = os.path.exists(os.path.join("campaigns", camp, "STOP_WORKER"))
print(f"plan specs (status != done): {len(specs)}")
print(f"  ... with a durable marker already on this box: {marker}")
print(f"  ... TRULY WORKABLE here: {len(specs) - marker}")
print(f"worker_state entries on box: {len(st)}")
print(f"STOP_WORKER present: {stop}")
try:
    print("limits file:", open("gamut_limits.json").read().strip())
except Exception as e:
    print("limits file: none", e)
