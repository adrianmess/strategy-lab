#!/usr/bin/env python3
"""Reconcile the HYPE+SUI spot FCFS combo vs its two solo entries."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "..", "dashboard")

WANT = ("Router_SPOT_HYPE_SUI_1m_fcfs_full", "Router_SPOT_HYPE_SUI_1m_fcfs_wf",
        "sp1w_hype1m_v7_volXtrend9_hB_cls_full",
        "sp1w_sui1m_v7_trend3_hA_cls_oosbest_full")

txt = open(os.path.join(DASH, "backtests.js")).read()
es = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
for e in es:
    if e["name"] not in WANT:
        continue
    s = e["stats"]
    tm = s.get("total_mult") or (s.get("final_eq", 1000) / 1000)
    tr = e.get("trades") or []
    first = tr[0]["entry_t"] if tr else "?"
    last = tr[-1]["exit_t"] if tr else "?"
    print(e["name"])
    print(f"   total {100 * (tm - 1):+.4g}% | "
          f"{s.get('monthly_growth_pct', 0):+.1f}%/mo | "
          f"months {s.get('months')} | n {s.get('n')} | "
          f"window {first} .. {last}")
