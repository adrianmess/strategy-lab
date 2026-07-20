import sys
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'optimizer'))
import _bootstrap as B
import numpy as np
import pandas as pd
import openpyxl
from common import load_segments
from rocx_engine import ROCX_DEFAULTS, precompute_rocx, run_rocx

XLSX = "/sessions/festive-happy-gauss/mnt/uploads/V4_ROC[-1]_45_Closed_-_Leveraged_ROC_(Lowest_Price_+_time_Frame)_and_SMA_Trend_Strategy_MEXC_SOLUSDT.P_2026-07-12.xlsx"
wb = openpyxl.load_workbook(XLSX, read_only=True)
rows = list(wb["Trades"].iter_rows(values_only=True))[1:]
ref = {}
for r in rows:
    d = ref.setdefault(r[0], {})
    if r[1].startswith("Entry"):
        d["et"], d["ep"] = r[2], r[4]
    else:
        d["xt"], d["xp"], d["sig"] = r[2], r[4], r[3]
ref = [ref[k] for k in sorted(ref)]

segs = load_segments()
g, d1 = segs[-1]
pre = precompute_rocx(g, d1)              # full-history warmup (security-style)
t = pd.to_datetime(pre["t"])
w = int(np.searchsorted(t.values, np.datetime64("2026-06-01T00:00")))
tr, eq, liq, op = run_rocx(pre, dict(ROCX_DEFAULTS), warmup=w, return_open=True)
tr = tr[tr.entry_idx >= w]
print(f"engine: {len(tr)} trades | open at end: {op is not None}")

dend = t.max()
SH = pd.Timedelta(hours=7)
ref_in = [x for x in ref if pd.Timestamp(x["et"]) + SH <= dend]
print(f"reference trades in window: {len(ref_in)}")
REASON = {0: "TP", 1: "trail", 2: "LIQ"}
n = 0
for k in range(max(len(ref_in), len(tr))):
    r_ = ref_in[k] if k < len(ref_in) else None
    e_ = tr.iloc[k] if k < len(tr) else None
    ok = (r_ is not None and e_ is not None
          and str(pd.Timestamp(r_["et"]) + SH)[:16] == e_["entry_t"].replace("T", " ")
          and abs(r_["ep"] - e_["entry"]) < 0.011
          and (("xt" not in r_) or str(pd.Timestamp(r_["xt"]) + SH)[:16] == e_["exit_t"].replace("T", " "))
          and (("xp" not in r_) or abs(r_["xp"] - e_["exit"]) < 0.011))
    if ok:
        n += 1
    else:
        print(f"#{k+1} MISMATCH")
        if r_ is not None:
            print(f"   ref: {r_['et']} @{r_['ep']} -> {r_.get('xt')} @{r_.get('xp')} ({r_.get('sig')})")
        if e_ is not None:
            print(f"   eng: {e_['entry_t']} @{e_['entry']:.2f} -> {e_['exit_t']} @{e_['exit']:.2f} ({REASON[int(e_['reason'])]})")
print(f"MATCHED {n}/{len(ref_in)} (engine {len(tr)})")
