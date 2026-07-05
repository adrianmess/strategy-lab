"""
Adaptive strategy variants built on the fast engine.

Key idea: the original strategy's frequency bottleneck is MACD thresholds
expressed in fixed price %/absolute units. We z-score MACD series against
their own trailing volatility (causal), making entry-trigger rates
regime-invariant. Then per-vol-regime knobs (thresholds, PT scale, leverage,
short trend-block) are optimized walk-forward.
"""
import numpy as np
import pandas as pd
from engine import DEFAULT_PARAMS
from fast_engine import params_to_vec, run_fast, PARAM_NAMES
from regimes import regime_features, vol_terciles, DAY

IDX = {k: i for i, k in enumerate(PARAM_NAMES)}

def roll_std(x, win):
    return pd.Series(x).rolling(win, min_periods=win // 4).std().to_numpy()

def make_adaptive_pre(pre, trend_block_z=None, zwin=3 * DAY):
    """Transform a precomputed dict so MACD thresholds are in z-units.
    - pre['macdL'] := z(macdL) * c  (engine tests macdL < thr*c  =>  z < thr)
    - pre['xMacd'] := z(xMacd)      (engine tests xMacd > thr    =>  z > thr)
    - optional: block shorts when trend z-score > trend_block_z
    Returns (new_pre, features)"""
    f = regime_features(pre)
    q = dict(pre)
    c = pre["c"]
    sL = roll_std(pre["macdL"], zwin)
    q["macdL"] = np.where(sL > 0, pre["macdL"] / sL, np.nan) * c
    sX = roll_std(pre["xMacd"], zwin)
    q["xMacd"] = np.where(sX > 0, pre["xMacd"] / sX, np.nan)
    if trend_block_z is not None:
        ok_short = ~(f["trend"] > trend_block_z)
        q["emaShortDown"] = pre["emaShortDown"] * ok_short
        q["xDn"] = pre["xDn"] * ok_short
    return q, f

def build_P(knobs, base=DEFAULT_PARAMS):
    """knobs: dict with per-regime lists [low, mid, high]:
       zL (neg), zS, zXS, ptScale, cdScale, lev
    Returns (3, NP) matrix."""
    rows = []
    for r in range(3):
        ov = {}
        ov["macdValPctLong"] = knobs["zL"][r]
        ov["macdValPctShort"] = knobs["zS"][r]
        ov["xMacdMinShort"] = knobs["zXS"][r]
        ps = knobs["ptScale"][r]
        for k in ["ptLong", "apt1Long", "apt2Long", "ptShort", "apt1Short", "apt2Short",
                  "xTpLong", "xApt1Long", "xApt2Long", "xTpShort", "xApt1Short", "xApt2Short"]:
            ov[k] = base[k] * ps
        cs = knobs["cdScale"][r]
        ov["cdPctLong"] = base["cdPctLong"] * cs
        ov["cdPctShort"] = base["cdPctShort"] * cs
        ov["xCdPctShort"] = base["xCdPctShort"] * cs
        ov["leverage"] = knobs["lev"][r]
        rows.append(params_to_vec(base, ov))
    return np.vstack(rows)

def slice_pre(pre, i0, i1):
    n = len(pre["c"])
    out = {}
    for k, v in pre.items():
        out[k] = v[i0:i1] if isinstance(v, np.ndarray) and len(v) == n else v
    return out

def run_adaptive(pres_adaptive, P, regimes, t0=None, t1=None, warmup=3000,
                 use_sl=False, liq_threshold=0.12, initial_capital=1000.0):
    """Run over all segments (optionally clipped to [t0,t1]), chaining equity.
    Live mode by default (no SL, liquidation possible)."""
    eq = initial_capital
    all_tr = []
    liq_any = False
    months = 0.0
    for (pre, f), reg in zip(pres_adaptive, regimes):
        t = pre["t"]
        i0, i1 = 0, len(t)
        if t0 is not None:
            i0 = int(np.searchsorted(t, np.datetime64(t0)))
        if t1 is not None:
            i1 = int(np.searchsorted(t, np.datetime64(t1)))
        if i1 - i0 < 100:
            continue
        # include warmup bars before i0 so indicators/cooldowns are warm
        w0 = max(0, i0 - warmup)
        sp = slice_pre(pre, w0, i1)
        rg = reg[w0:i1]
        tr, eq, liq = run_fast(sp, P, regime=rg, warmup=i0 - w0,
                               initial_capital=eq, use_sl=use_sl,
                               liq_threshold=liq_threshold)
        months += (i1 - i0) / (DAY * 30.4)
        all_tr.append(tr)
        if liq:
            liq_any = True
            break
    trades = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    return trades, eq, liq_any, months
