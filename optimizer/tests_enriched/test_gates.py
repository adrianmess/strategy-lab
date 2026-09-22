"""Gate regression: with zero masks a classic run is unchanged; with a
direction masked, that direction never trades. Run from optimizer/:
    LAB_COIN=sol LAB_MARKET=perp LAB_TF=3 python3 tests_enriched/test_gates.py
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.dirname(HERE)
os.chdir(OPT)
sys.path.insert(0, OPT)
sys.path.insert(0, os.path.join(OPT, "..", "adaptive_trader", "research2"))
sys.path.insert(0, os.path.join(OPT, "..", "adaptive_trader", "research"))
import _bootstrap  # noqa: E402,F401
import wf2 as W  # noqa: E402
from macdx_engine import run_macdx_P  # noqa: E402

cfg = json.load(open("runs/s3w_sol3m_macdx_volXtrend9_hBtw_und/holdout_best_config.json"))
cand, method = cfg["cand"], cfg["method"]
G = W.load_globals(("v6", "macdx"))
R = G["nreg"][method]
P = W.build_P_macdx(cand, R)
pre = G["macdx"][0]
reg = G["regimes_v6"][method][0]
n = len(pre["c"])


def count(**kw):
    tr = run_macdx_P(pre, P, regime=reg, warmup=3000, commission=0.0008, **kw)
    tr = tr[0] if isinstance(tr, tuple) else tr
    d = tr["dir"].values if len(tr) else np.array([])
    return len(tr), int((d > 0).sum()), int((d < 0).sum())


ones = np.ones(n, np.int8)
print("segment 0 bars:", n)
base = count()
print("no masks      : trades=%d longs=%d shorts=%d" % base)
nl = count(no_long=ones)
print("no_long=all   : trades=%d longs=%d shorts=%d" % nl)
ns = count(no_short=ones)
print("no_short=all  : trades=%d longs=%d shorts=%d" % ns)
both = count(no_long=ones, no_short=ones)
print("both masked   : trades=%d longs=%d shorts=%d" % both)
ok = nl[1] == 0 and ns[2] == 0 and both[0] == 0 and base[0] > 0
print("GATES OK" if ok else "GATES BROKEN")
sys.exit(0 if ok else 1)
