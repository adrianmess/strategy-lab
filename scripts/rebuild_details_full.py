#!/usr/bin/env python3
"""One-time: rebuild dashboard/bt_detail/<name>.json at FULL fidelity from
(a) per-run payloads runs/<run>/bts/*.json (gamut era, synced from the box)
and (b) the pre-prune backup /tmp/backtests_backup.js (older entries).
The main backtests.js list is not touched."""
import glob, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
DET = os.path.join(HERE, "..", "dashboard", "bt_detail")
os.makedirs(DET, exist_ok=True)


def write(name, curve, trades):
    with open(os.path.join(DET, name + ".json"), "w") as f:
        json.dump(dict(curve=curve or [], trades=trades or []), f)


n_bts = 0
for p in glob.glob(os.path.join(HERE, "..", "optimizer", "runs", "*", "bts",
                                "*.json")):
    try:
        e = json.load(open(p))
        if e.get("name"):
            write(e["name"], e.get("curve"), e.get("trades"))
            n_bts += 1
    except Exception:
        pass
print(f"from bts payloads: {n_bts}")

n_bak = 0
bak = "/tmp/backtests_backup.js"
if os.path.exists(bak):
    txt = open(bak).read()
    entries = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
    del txt
    done = {os.path.basename(f)[:-5] for f in os.listdir(DET)}
    for e in entries:
        nm = e.get("name")
        # bts payloads are newer/authoritative — only fill entries they lack
        if nm and nm not in done and (e.get("curve") or e.get("trades")):
            write(nm, e.get("curve"), e.get("trades"))
            n_bak += 1
print(f"from backup (older entries): {n_bak}")
print(f"total detail files: {len(os.listdir(DET))}")
