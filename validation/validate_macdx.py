import sys, os
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'optimizer'))
import _bootstrap as B
import numpy as np
import pandas as pd
import openpyxl

from common import load_segments
from macdx_engine import MACDX_DEFAULTS, precompute_macdx, run_macdx

XLSX = "/sessions/festive-happy-gauss/mnt/uploads/MACD_Crossover_Strategy_with_TP,_SL,_Time,_MACD_Thresholds_+_Histogram_Filter_+_Leverage_MEXC_SOLUSDT.P_2026-07-12.xlsx"

# ---- reference trades ----
wb = openpyxl.load_workbook(XLSX, read_only=True)
rows = list(wb["Trades"].iter_rows(values_only=True))[1:]
ref = {}
for r in rows:
    tn, typ, dt, sigl, px = r[0], r[1], r[2], r[3], r[4]
    d = ref.setdefault(tn, {})
    if typ.startswith("Entry"):
        d["dir"] = 1 if "long" in typ else -1
        d["entry_t"], d["entry"] = dt, px
    else:
        d["exit_t"], d["exit"], d["sig"] = dt, px, sigl
ref = [ref[k] for k in sorted(ref)]

# ---- our data, sliced to the TV backtest window ----
segs = load_segments()
g, d1 = segs[-1]
start = pd.Timestamp("2026-06-01 00:00:00")
g = g[g["t"] >= start].reset_index(drop=True)
d1 = d1[d1["t"] >= start].reset_index(drop=True)
print("bars:", len(g), g["t"].min(), "->", g["t"].max())

p = dict(MACDX_DEFAULTS)
pre = precompute_macdx(g, d1, p)
tr, eq, liq, op = run_macdx(pre, p, warmup=0, return_open=True)
print(f"engine: {len(tr)} trades, final eq {eq:.2f}, liq={liq}, open={op is not None}")

# ---- compare on the overlapping window ----
data_end = g["t"].max()
ref_in = [x for x in ref if pd.Timestamp(x["entry_t"])+pd.Timedelta(hours=7) <= data_end]
print(f"reference trades inside data window: {len(ref_in)}")

REASON = {0: "PT", 1: "SL", 2: "LIQ", 3: "flip"}
n_match = 0
for k in range(max(len(ref_in), len(tr))):
    r_ = ref_in[k] if k < len(ref_in) else None
    e_ = tr.iloc[k] if k < len(tr) else None
    ok = (r_ is not None and e_ is not None
          and str(pd.Timestamp(r_["entry_t"])+pd.Timedelta(hours=7))[:16] == e_["entry_t"].replace("T"," ")
          and r_["dir"] == e_["dir"]
          and abs(r_["entry"] - e_["entry"]) < 0.011
          and (("exit_t" not in r_) or str(pd.Timestamp(r_["exit_t"])+pd.Timedelta(hours=7))[:16] == e_["exit_t"].replace("T"," "))
          and (("exit" not in r_) or abs(r_["exit"] - e_["exit"]) < 0.011))
    if ok:
        n_match += 1
    else:
        print(f"#{k+1} MISMATCH")
        if r_ is not None:
            print(f"   ref: {'L' if r_['dir']>0 else 'S'} {r_['entry_t']} @{r_['entry']}"
                  f" -> {r_.get('exit_t')} @{r_.get('exit')} ({r_.get('sig')})")
        if e_ is not None:
            print(f"   eng: {'L' if e_['dir']>0 else 'S'} {e_['entry_t']} @{e_['entry']:.2f}"
                  f" -> {e_['exit_t']} @{e_['exit']:.2f} ({REASON[int(e_['reason'])]})")
        if k > 6 and n_match < k - 6:
            print("   … stopping after repeated mismatches"); break
print(f"MATCHED {n_match}/{len(ref_in)} (engine produced {len(tr)})")
