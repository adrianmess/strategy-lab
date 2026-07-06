"""
Optimizer v2 — full parameter search (thresholds AND indicator lengths) on
engine3 ("V7"), with per-regime specialist parameter sets and multiple
search algorithms:

  random  : uniform sampling of the whole space (baseline; hard to fool)
  genetic : population evolution — crossover + mutation, elitism
  refine  : hill-climbing from the best known candidates (local polish)

A candidate is {regime r: {param: value}} — with a regime method active,
EVERY parameter (including RSI/MACD/BB/EMA lengths) may differ per
low/mid/high bucket. Feasibility and scoring mirror wf2 (MTM drawdown, no
liquidation, robust monthly-growth score).
"""
import json, os
import numpy as np
import pandas as pd

from engine3 import get_pres3, run3, vec3, P3_NAMES, VARIANTS, C
from regimes import make_regimes, DAY
from wf2 import mtm_curve
from adaptive import slice_pre

FUT_COMM = 0.0004
SPOT_COMM = 0.0005

_G3 = {}

def load_g3():
    if _G3:
        return _G3
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "optimizer", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    pres = get_pres3(cache=os.path.join(cache_dir, "engine3_pre.pkl"))
    _G3["pres"] = pres
    _G3["regimes"] = {}
    for m in ["none", "vol3", "vol3_7d", "volume3", "trend3", "volXtrend9"]:
        rs, R = [], 1
        for pre in pres:
            r, R = make_regimes(pre["feats"], m)
            rs.append(r)
        _G3["regimes"][m] = (rs, R)
    return _G3

def load_space(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "optimizer", "param_space.json")
    return json.load(open(path))["v7"]

# ---------------- sampling / genome ops ----------------

def sample_regime_params(rng, space, mode):
    d = {}
    for k, spec in space["continuous"].items():
        lo, hi = spec["range"]
        d[k] = float(rng.uniform(lo, hi))
    for k, spec in space["menus"].items():
        d[k] = float(rng.choice(spec["options"]))
    for k in space["flags"]:
        d[k] = float(rng.random() < 0.75)
    # orderings the engine expects
    if d["apt1Long"] > d["ptLong"]: d["apt1Long"] = d["ptLong"] * 0.7
    if d["apt2Long"] > d["apt1Long"]: d["apt2Long"] = d["apt1Long"] * 0.6
    if d["apt1Short"] > d["ptShort"]: d["apt1Short"] = d["ptShort"] * 0.7
    if d["apt2Short"] > d["apt1Short"]: d["apt2Short"] = d["apt1Short"] * 0.6
    if d["dur2Long"] < d["dur1Long"]: d["dur1Long"], d["dur2Long"] = d["dur2Long"], d["dur1Long"]
    if d["dur2Short"] < d["dur1Short"]: d["dur1Short"], d["dur2Short"] = d["dur2Short"], d["dur1Short"]
    if d["xDur2Long"] < d["xDur1Long"]: d["xDur1Long"], d["xDur2Long"] = d["xDur2Long"], d["xDur1Long"]
    if d["xDur2Short"] < d["xDur1Short"]: d["xDur1Short"], d["xDur2Short"] = d["xDur2Short"], d["xDur1Short"]
    if mode == "spot":
        d["leverage"] = 1.0
        d["eS3"] = 0.0; d["eXS"] = 0.0
    return d

def sample_candidate(rng, space, R, mode, per_regime=True):
    if per_regime:
        regs = [sample_regime_params(rng, space, mode) for _ in range(R)]
    else:
        base = sample_regime_params(rng, space, mode)
        regs = [dict(base) for _ in range(R)]
    return dict(strategy="v7", mode=mode, regs=regs)

def crossover(rng, a, b):
    child = dict(strategy="v7", mode=a["mode"], regs=[])
    for ra, rb in zip(a["regs"], b["regs"]):
        child["regs"].append({k: (ra[k] if rng.random() < 0.5 else rb[k]) for k in ra})
    return child

def mutate(rng, cand, space, mode, p_cont=0.25, p_menu=0.10, sigma=0.10):
    out = dict(strategy="v7", mode=cand["mode"], regs=[])
    for reg in cand["regs"]:
        d = dict(reg)
        for k, spec in space["continuous"].items():
            if rng.random() < p_cont:
                lo, hi = spec["range"]
                d[k] = float(np.clip(d[k] + rng.normal(0, sigma * (hi - lo)), lo, hi))
        for k, spec in space["menus"].items():
            if rng.random() < p_menu:
                d[k] = float(rng.choice(spec["options"]))
        for k in space["flags"]:
            if rng.random() < 0.05:
                d[k] = 1.0 - d[k]
        if mode == "spot":
            d["leverage"] = 1.0; d["eS3"] = 0.0; d["eXS"] = 0.0
        out["regs"].append(d)
    return out

def build_P3(cand):
    return np.vstack([vec3(reg) for reg in cand["regs"]])

# ---------------- evaluation ----------------

def eval3(cand, method, t0=None, t1=None, warmup=3000):
    G = load_g3()
    regs_list, R = G["regimes"][method]
    P = build_P3(cand)
    if P.shape[0] != R:  # allow single-set candidates on any method
        P = np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
    mode = cand["mode"]
    use_sl = (mode == "spot")
    comm = FUT_COMM if mode == "lev" else SPOT_COMM
    eq = 1000.0
    months = 0.0
    all_tr = []
    mtm_dd = 0.0
    liq_any = False
    for pre, reg in zip(G["pres"], regs_list):
        t = pre["t"]
        i0 = 0 if t0 is None else int(np.searchsorted(t, np.datetime64(t0)))
        i1 = len(t) if t1 is None else int(np.searchsorted(t, np.datetime64(t1)))
        i0 = max(i0, warmup)
        if i1 - i0 < 200:
            continue
        w0 = max(0, i0 - warmup)
        sp = slice_pre(pre, w0, i1)
        eq_before = eq
        tr, eq, liq = run3(sp, P, regime=reg[w0:i1], warmup=i0 - w0,
                           initial_capital=eq, commission=comm,
                           use_sl=use_sl, dyn_liq=(mode == "lev"))
        months += (i1 - i0) / (DAY * 30.4)
        all_tr.append(tr)
        if len(tr):
            _, dseg = mtm_curve(tr, sp["c"], initial=eq_before)
            mtm_dd = max(mtm_dd, dseg)
        if liq:
            liq_any = True
            break
    if months <= 0:
        return None
    tr = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    if len(tr) == 0:
        return None
    growth = np.log(max(eq, 1e-9) / 1000.0) / months
    e = tr["net"].cumsum() + 1000.0
    mo = pd.to_datetime(tr["exit_t"]).dt.to_period("M")
    lg = np.log(np.maximum(e.to_numpy(), 1e-9))
    gm = pd.DataFrame(dict(mo=mo, lg=lg)).groupby("mo")["lg"].last().diff().dropna()
    g_mean = float(gm.mean()) if len(gm) >= 2 else growth
    g_std = float(gm.std()) if len(gm) >= 2 else 0.0
    return dict(n=len(tr), months=months, eq=float(eq), growth=float(growth),
                liq=liq_any, maxdd=float(mtm_dd), tpm=len(tr) / months,
                sl_hits=int((tr["reason"] == 1).sum()),
                worst_mae=float(tr["mae"].min()),
                win=float((tr["net"] > 0).mean()),
                score=g_mean - 0.25 * g_std)

def feasible3(m, mode, min_tpm=2.0, min_n=10):
    if m is None or m["liq"]:
        return False
    if m["n"] < min_n or m["tpm"] < min_tpm:
        return False
    cap = 0.80 if mode == "lev" else 0.50
    if m["maxdd"] > cap:
        return False
    return True

# ---------------- algorithms (single-process batch APIs) ----------------

def batch_random(rng, space, R, mode, method, n, t0, t1, per_regime=True):
    out = []
    for _ in range(n):
        c = sample_candidate(rng, space, R, mode, per_regime)
        m = eval3(c, method, t0, t1)
        if feasible3(m, mode):
            out.append((m["score"], c, m))
    return out

def batch_offspring(rng, space, mode, method, parents, n, t0, t1):
    """Genetic step: produce and evaluate n children from a parent pool."""
    out = []
    for _ in range(n):
        if len(parents) >= 2:
            a, b = rng.choice(len(parents), 2, replace=False)
            child = crossover(rng, parents[a], parents[b])
        else:
            child = parents[0]
        child = mutate(rng, child, space, mode)
        m = eval3(child, method, t0, t1)
        if feasible3(m, mode):
            out.append((m["score"], child, m))
    return out

def batch_refine(rng, space, mode, method, seed_cand, n, t0, t1, sigma=0.04):
    out = []
    for _ in range(n):
        child = mutate(rng, seed_cand, space, mode, p_cont=0.15, p_menu=0.04, sigma=sigma)
        m = eval3(child, method, t0, t1)
        if feasible3(m, mode):
            out.append((m["score"], child, m))
    return out
