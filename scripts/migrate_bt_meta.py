#!/usr/bin/env python3
"""One-time: move rating/best fields out of backtests.js into bt_meta.json."""
import json, os, fcntl
HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "..", "dashboard", "backtests.js")
M = os.path.join(HERE, "..", "dashboard", "bt_meta.json")
with open(P + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    txt = open(P).read()
    entries = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
    del txt
    meta = {}
    if os.path.exists(M):
        meta = json.load(open(M))
    for e in entries:
        m = meta.get(e["name"], {})
        r = e.pop("rating", None)
        b = e.pop("best", None)
        if r: m["rating"] = r
        if b: m["best"] = True
        if m: meta[e["name"]] = m
    json.dump(meta, open(M, "w"))
    tmp = P + ".tmpmig"
    with open(tmp, "w") as f:
        f.write("window.BACKTESTS = "); json.dump(entries, f); f.write(";")
    os.replace(tmp, P)
print(f"migrated: {len(meta)} entries carry rating/best in bt_meta.json")
