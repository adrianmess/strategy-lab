"""Smoke test for the feature-native families: sample candidates, run the
shared core on every segment, and report trades / reasons per family.
Run from optimizer/:
    LAB_COIN=sui LAB_MARKET=perp LAB_TF=3 python3 tests_enriched/test_families.py
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.dirname(HERE)
os.chdir(OPT)
sys.path.insert(0, OPT)
sys.path.insert(0, os.path.join(OPT, "..", "adaptive_trader", "research2"))
sys.path.insert(0, os.path.join(OPT, "..", "adaptive_trader", "research"))
os.environ["LAB_FEATURES"] = "ob,cvd,vp,oi"
os.environ["LAB_FUNDING"] = "1"
import _bootstrap  # noqa: E402,F401
import wf2 as W  # noqa: E402
import enriched as E  # noqa: E402
from enriched_engine import ENR_FAMILIES, run_enr_P, sample_enr, build_P_enr  # noqa: E402

print("coverage:", E.coverage())
G = W.load_globals(("v6", "macdx"))
R = G["nreg"]["vol3"]
regs = G["regimes_v6"]["vol3"]
rng = np.random.default_rng(7)
ok_all = True
for fam in ENR_FAMILIES:
    tot = 0; reasons = {}; t0 = time.time(); liqs = 0
    for k in range(6):
        c = sample_enr(fam)(rng, R, "lev", None)
        P = build_P_enr(c, R)
        for pre, reg in zip(G["macdx"], regs):
            assert "f_ok" in pre, "features not attached"
            tr, eq, liq, op = run_enr_P(pre, P, fam, regime=reg, warmup=3000,
                                        commission=0.0008, return_open=True,
                                        **E.gate_kwargs(pre, reg, c))
            E.charge_funding(tr, pre, "lev")
            tot += len(tr); liqs += int(liq)
            for r_ in tr["reason"].values:
                reasons[int(r_)] = reasons.get(int(r_), 0) + 1
            if len(tr):
                assert np.isfinite(tr["net"].values).all()
                assert (tr["exit_idx"].values > tr["entry_idx"].values).all()
                assert "funding" in tr
    print(f"{fam:10s} trades={tot:6d} liq={liqs} reasons={reasons} "
          f"({time.time()-t0:.1f}s incl. jit)")
    # eval_config end-to-end (score path)
    c = sample_enr(fam)(rng, R, "lev", None)
    m = W.eval_config(c, "vol3", "lev", None, None)
    print(f"{'':10s} eval_config: {None if m is None else dict(n=m.get('n'), growth=m.get('growth'))}")
print("FAMILIES OK" if ok_all else "FAMILIES BROKEN")
