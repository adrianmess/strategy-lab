"""Shared data loading + segment handling + precompute caching.

Data files resolve to $COINDATA_DIR, then <this module's dir>/data, then the
current directory (legacy). Caches are written to the CWD so each optimizer
run directory keeps its own.
"""
import numpy as np
import pandas as pd
import pickle, os
from engine import DEFAULT_PARAMS
from fast_engine import precompute, params_to_vec, run_fast

CACHE = "precomputed.pkl"

def _data_path(fname):
    for base in [os.environ.get("COINDATA_DIR"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                 "."]:
        if base and os.path.exists(os.path.join(base, fname)):
            return os.path.join(base, fname)
    raise FileNotFoundError(fname)

def load_segments():
    df3 = pd.read_parquet(_data_path("sol_3min.parquet"))
    df1 = pd.read_parquet(_data_path("sol_1min.parquet"))
    df3["t"] = df3["t"].dt.tz_localize(None)
    df1["t"] = df1["t"].dt.tz_localize(None)
    d = df3["t"].diff().dt.total_seconds().div(60).fillna(3)
    seg_id = (d > 1000).cumsum()
    segs = []
    for _, g in df3.groupby(seg_id):
        g = g.reset_index(drop=True)
        if len(g) < 5000:
            continue
        lo, hi = g["t"].min(), g["t"].max()
        d1 = df1[(df1["t"] >= lo) & (df1["t"] <= hi + pd.Timedelta(minutes=3))].reset_index(drop=True)
        segs.append((g, d1))
    return segs

def get_pres(force=False):
    if os.path.exists(CACHE) and not force:
        try:
            with open(CACHE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"cache {CACHE} unreadable ({e}); rebuilding...")
            try: os.remove(CACHE)
            except OSError: pass
    pres = [precompute(g, d1) for g, d1 in load_segments()]
    with open(CACHE, "wb") as f:
        pickle.dump(pres, f)
    return pres

def run_on_all(pres, P, regimes=None, warmup=3000, use_sl=True, liq_threshold=0.12,
               initial_capital=1000.0):
    """Run across segments, chaining equity. regimes: list of per-segment arrays or None."""
    all_tr = []
    eq = initial_capital
    liq_any = False
    for k, pre in enumerate(pres):
        reg = None if regimes is None else regimes[k]
        tr, eq, liq = run_fast(pre, P, regime=reg, warmup=warmup,
                               initial_capital=eq, use_sl=use_sl,
                               liq_threshold=liq_threshold)
        all_tr.append(tr)
        if liq:
            liq_any = True
            break
    trades = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    return trades, eq, liq_any

def metrics(trades, eq, liq, initial_capital=1000.0, label=""):
    if len(trades) == 0:
        return dict(label=label, n=0, eq=eq, liq=liq)
    m = dict(label=label,
             n=len(trades),
             eq=eq,
             ret_mult=eq / initial_capital,
             liq=liq,
             sl_hits=int((trades["reason"] == 1).sum()),
             worst_mae=float(trades["mae"].min()),
             mae_p99=float(trades["mae"].quantile(0.01)),
             win_rate=float((trades["net"] > 0).mean()),
             )
    return m
