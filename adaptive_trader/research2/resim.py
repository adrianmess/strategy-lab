#!/usr/bin/env python3
"""Continuous OOS re-simulation.

Takes a config's per-fold chosen candidates and runs ONE continuous simulation
over the whole OOS span: parameters switch at refit dates (via stacked regime
indices), positions and equity carry across fold boundaries. This is the
honest number — per-fold evaluation truncates open positions at boundaries.
"""
import json, os, glob
import numpy as np
import pandas as pd

def mtm_curve(trades: pd.DataFrame, closes: np.ndarray, seg_offset: int = 0,
              initial: float = 1000.0):
    """Mark-to-market equity per bar reconstructed from the trade list.
    trades entry_idx/exit_idx are indices into `closes` (already offset).
    Returns (equity_series indexed by bar, max_drawdown)."""
    n = len(closes)
    eq_closed = initial
    mtm = np.full(n, np.nan)
    last = 0
    for _, tr in trades.iterrows():
        i0, i1 = int(tr["entry_idx"]), int(tr["exit_idx"])
        mtm[last:i0] = eq_closed
        seg = slice(i0, min(i1 + 1, n))
        unreal = tr["qty"] * (closes[seg] - tr["entry"]) * tr["dir"]
        mtm[seg] = eq_closed + unreal
        eq_closed += tr["net"]
        last = min(i1 + 1, n)
    mtm[last:] = eq_closed
    cummax = np.maximum.accumulate(np.where(np.isnan(mtm), -np.inf, mtm))
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = (cummax - mtm) / cummax
    return mtm, float(np.nanmax(dd))

import wf2 as _W
from wf2 import load_globals, build_P_v6, build_P_scalpx, FUT_COMM, SPOT_COMM
from fast_engine import run_fast, PARAM_NAMES
from scalp_engine import run_scalp, SCALP_PARAM_NAMES
from regimes import DAY

NPV = len(PARAM_NAMES)
NSP = len(SCALP_PARAM_NAMES)

def disabled_row_v6():
    from engine import DEFAULT_PARAMS
    from fast_engine import params_to_vec
    return params_to_vec(DEFAULT_PARAMS, dict(enableLong3m=0, enableShort3m=0,
                                              enableLongX=0, enableShortX=0))

def disabled_row_scalp():
    from scalp_engine import scalp_vec
    return scalp_vec(dict(enableLong=0, enableShort=0))

def resim_config(cid, base="wf2_results"):
    strategy, mode, method, window = cid.split("__")
    G = load_globals((strategy,))
    R = G["nreg"][method]
    folds = {}
    for p in sorted(glob.glob(os.path.join(base, cid, "fold_*.json"))):
        f = json.load(open(p))
        if f["status"] == "ok":
            folds[f["job"]["fold_idx"]] = f["cand"]
    K = len(_W.REFIT_DATES)
    # stacked P: rows [k*R + r] for fold k; final row = disabled
    build = build_P_v6 if strategy == "v6" else build_P_scalpx
    dis = disabled_row_v6() if strategy == "v6" else disabled_row_scalp()
    rows = []
    for k in range(K):
        if k in folds:
            rows.append(build(folds[k], R))
        else:
            rows.append(np.vstack([dis] * R))
    rows.append(dis.reshape(1, -1))
    P2 = np.vstack(rows)
    dis_idx = K * R

    fold_starts = np.array([np.datetime64(d) for d in _W.REFIT_DATES])
    fold_ends = np.array([np.datetime64(d + pd.Timedelta(days=_W.TEST_DAYS)) for d in _W.REFIT_DATES])
    oos0, oos1 = fold_starts[0], fold_ends[-1]

    segs = G["v6"][0] if strategy == "v6" else G["scalp"]
    regs = G["regimes_v6" if strategy == "v6" else "regimes_sc"][method]
    # for v6, honor each fold's trend-variant? candidates store tv; continuous
    # sim uses tv=0 arrays for simplicity when folds disagree; use majority tv.
    if strategy == "v6":
        tvs = [c.get("tv", 0) for c in folds.values()]
        tv = int(round(np.mean(tvs))) if tvs else 0
        segs = G["v6"][min(tv, len(G["v6"]) - 1)]

    eq = 1000.0
    all_tr = []
    curve = []
    liq_any = False
    mtm_dd = 0.0
    warmup = 3000 if strategy == "v6" else 2500
    for (pre, f), reg in zip(segs, regs):
        t = pre["t"]
        i0 = int(np.searchsorted(t, oos0))
        i1 = int(np.searchsorted(t, oos1))
        if i1 - i0 < 100:
            continue
        w0 = max(0, i0 - warmup)
        # per-bar fold index
        k_arr = np.searchsorted(fold_starts, t[w0:i1], side="right") - 1
        valid = (k_arr >= 0) & (t[w0:i1] < fold_ends[np.clip(k_arr, 0, K - 1)])
        reg2 = np.where(valid, k_arr * R + reg[w0:i1], dis_idx).astype(np.int32)
        from adaptive import slice_pre
        sp = slice_pre(pre, w0, i1)
        eq_before = eq
        if strategy == "v6":
            tr, eq, liq = run_fast(sp, P2, regime=reg2, warmup=i0 - w0,
                                   initial_capital=eq,
                                   use_sl=(mode == "spot"),
                                   commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                   liq_threshold=-1.0 if mode == "lev" else 1e9)
        else:
            tr, eq, liq = run_scalp(sp, P2, regime=reg2, warmup=i0 - w0,
                                    initial_capital=eq,
                                    commission=FUT_COMM if mode == "lev" else SPOT_COMM,
                                    liq_threshold=-1.0 if mode == "lev" else 1e9)
        if len(tr):
            mtm, dd_seg = mtm_curve(tr, sp["c"], initial=eq_before)
            mtm_dd = max(mtm_dd, dd_seg)
            step = max(1, len(mtm) // 400)
            ts = pd.to_datetime(sp["t"][::step])
            for x, v in zip(ts, mtm[::step]):
                if np.isfinite(v):
                    curve.append(dict(t=str(x), eq=float(v)))
        all_tr.append(tr)
        if liq:
            liq_any = True
            break
    tr = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    months = (pd.Timestamp(oos1) - pd.Timestamp(oos0)).days / 30.4
    if len(tr) == 0:
        return dict(cid=cid, status="empty")
    e = tr["net"].cumsum() + 1000.0
    dd_closed = float(((e.cummax() - e) / e.cummax()).max())
    growth = np.log(max(eq, 1e-9) / 1000.0) / months
    return dict(cid=cid, status="ok", months=months, final_eq=float(eq),
                total_mult=float(eq / 1000.0),
                monthly_growth_pct=float((np.exp(growth) - 1) * 100),
                liq=liq_any, maxdd_closed=dd_closed, maxdd_mtm=mtm_dd,
                n=len(tr), tpm=len(tr) / months,
                sl_hits=int((tr["reason"] == 1).sum()),
                worst_mae=float(tr["mae"].min()),
                win=float((tr["net"] > 0).mean()),
                curve=curve)

def main():
    out = []
    for cid in sorted(os.listdir("wf2_results")):
        r = resim_config(cid)
        out.append(r)
        print(cid, {k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in r.items() if k != "curve"}, flush=True)
    json.dump(out, open("resim_results.json", "w"), default=float)

if __name__ == "__main__":
    main()
