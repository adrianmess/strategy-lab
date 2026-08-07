#!/usr/bin/env python3
"""One-time: slim backtests.js — move curve+config into bt_detail files,
precompute wk_ok/mo_ok period flags from the FULL curves. 177MB -> ~30MB."""
import json, os, fcntl, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prune_backtests import period_flags, DETAIL_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "..", "dashboard", "backtests.js")

with open(P + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    txt = open(P).read()
    entries = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
    del txt
    os.makedirs(DETAIL_DIR, exist_ok=True)
    n_flag = n_cfg = 0
    for e in entries:
        nm = e.get("name")
        if not nm:
            continue
        dp = os.path.join(DETAIL_DIR, nm + ".json")
        det = None
        if os.path.exists(dp):
            try:
                det = json.load(open(dp))
            except Exception:
                det = None
        if det is None:
            det = dict(curve=e.get("curve") or [], trades=e.get("trades") or [])
        full_curve = det.get("curve") or e.get("curve") or []
        # flags from the FULL curve (more accurate than the old 200-pt one)
        if e.get("wk_ok") is None:
            wk, mo = period_flags(full_curve)
            e["wk_ok"], e["mo_ok"] = wk, mo
            n_flag += 1
        if e.get("config") is not None:
            det["config"] = e.get("config")
            n_cfg += 1
        with open(dp, "w") as f:
            json.dump(det, f)
        e.pop("curve", None)
        e.pop("config", None)
        e["trades"] = []
        e["detail"] = True
    tmp = P + ".tmpslim"
    with open(tmp, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f)
        f.write(";")
    os.replace(tmp, P)
print(f"flags computed: {n_flag}, configs moved: {n_cfg}, "
      f"size now {os.path.getsize(P)//1048576} MB, entries {len(entries)}")
