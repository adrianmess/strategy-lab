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
_TFM = float(os.environ.get("LAB_TF", "3"))
import pandas as pd

from engine3 import get_pres3, run3, vec3
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
    _coin = os.environ.get("LAB_COIN", "sol").lower()
    if _coin != "sol":
        cache_dir = os.path.join(cache_dir, _coin)
    if os.environ.get("LAB_MARKET", "perp").lower() == "spot":
        cache_dir = os.path.join(cache_dir, "spotdata")
    if os.environ.get("LAB_TF", "3") != "3":
        cache_dir = os.path.join(cache_dir, f'tf{os.environ.get("LAB_TF")}')
    os.makedirs(cache_dir, exist_ok=True)
    # re-imported deliberately, not a leftover: engine3.VARIANTS can be
    # rebuilt after this module was imported, and the module-level binding
    # would still point at the old list
    from engine3 import variants_hash, VARIANTS, _DEFAULT_VARIANTS
    h = variants_hash()
    cache = os.path.join(cache_dir, f"engine3_pre_{h}.pkl")
    legacy = os.path.join(cache_dir, "engine3_pre.pkl")
    if not os.path.exists(cache) and os.path.exists(legacy) \
            and VARIANTS == _DEFAULT_VARIANTS:
        try: os.rename(legacy, cache)   # default lists: reuse the old cache
        except OSError: pass
    if not os.path.exists(cache):
        print("indicator-length libraries changed (or first build) — "
              "precomputing variants, this can take a few minutes...", flush=True)
    pres = get_pres3(cache=cache)
    _G3["pres"] = pres
    _G3["regimes"] = {}
    for m in ["none", "vol3", "vol3_7d", "volume3", "trend3", "volXtrend9",
              "cvol7"]:
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
    for k, spec in space["flags"].items():
        fx = spec.get("fixed") if isinstance(spec, dict) else None
        d[k] = float(fx) if fx is not None else float(rng.random() < 0.75)
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
    else:
        # INTEGER LEVERAGE: MEXC only accepts whole numbers; search only
        # configs the executor can set exactly as backtested
        d["leverage"] = max(1.0, float(int(d["leverage"])))   # floor
    return d

def _interp_ends(lo_d, hi_d, R, space, mode):
    """Continuous adaptation (cadapt): materialize R per-grade param dicts by
    interpolating between the CALM endpoint (grade 0) and the VOLATILE
    endpoint (grade R-1). Continuous params lerp; menu params lerp in
    option-INDEX space (rounded), so indicator lengths morph through the
    variant grid; flags step at the midpoint. The searcher only ever touches
    the two endpoints — fewer knobs than vol3's three independent sets."""
    regs = []
    menu_opts = {k: [float(o) for o in spec["options"]]
                 for k, spec in space["menus"].items()}
    for g in range(R):
        t = g / max(R - 1, 1)
        d = {}
        for k in set(lo_d) | set(hi_d):
            a, b = lo_d.get(k, hi_d.get(k)), hi_d.get(k, lo_d.get(k))
            if k in space["continuous"]:
                d[k] = float(a + (b - a) * t)
            elif k in menu_opts:
                opts = menu_opts[k]
                ia = min(range(len(opts)), key=lambda i: abs(opts[i] - a))
                ib = min(range(len(opts)), key=lambda i: abs(opts[i] - b))
                d[k] = opts[int(round(ia + (ib - ia) * t))]
            else:                                   # flags
                d[k] = float(a if t < 0.5 else b)
        if mode == "spot":
            d["leverage"] = 1.0; d["eS3"] = 0.0; d["eXS"] = 0.0
        elif "leverage" in d:
            d["leverage"] = max(1.0, float(int(d["leverage"])))
        regs.append(d)
    return regs

def sample_candidate(rng, space, R, mode, per_regime=True):
    if per_regime == "ends":
        lo_d = sample_regime_params(rng, space, mode)
        hi_d = sample_regime_params(rng, space, mode)
        return dict(strategy="v7", mode=mode, cadapt=True, ends=[lo_d, hi_d],
                    regs=_interp_ends(lo_d, hi_d, R, space, mode))
    if per_regime:
        regs = [sample_regime_params(rng, space, mode) for _ in range(R)]
    else:
        base = sample_regime_params(rng, space, mode)
        regs = [dict(base) for _ in range(R)]
    return dict(strategy="v7", mode=mode, regs=regs)

def crossover(rng, a, b):
    if a.get("cadapt") and b.get("cadapt"):
        R = len(a["regs"])
        space = load_space()
        ends = []
        for ea, eb in zip(a["ends"], b["ends"]):
            keys = set(ea) | set(eb)
            ends.append({k: (ea[k] if (k not in eb or (k in ea and rng.random() < 0.5))
                             else eb[k]) for k in keys})
        return dict(strategy="v7", mode=a["mode"], cadapt=True, ends=ends,
                    regs=_interp_ends(ends[0], ends[1], R, space, a["mode"]))
    child = dict(strategy="v7", mode=a["mode"], regs=[])
    for ra, rb in zip(a["regs"], b["regs"]):
        keys = set(ra) | set(rb)
        d = {k: (ra[k] if (k not in rb or (k in ra and rng.random() < 0.5))
                 else rb[k]) for k in keys}
        if a["mode"] != "spot" and "leverage" in d:   # integer leverage (see sampler)
            d["leverage"] = max(1.0, float(int(d["leverage"])))   # floor
        child["regs"].append(d)
    return child

def mutate(rng, cand, space, mode, p_cont=0.25, p_menu=0.10, sigma=0.10):
    if cand.get("cadapt"):
        R = len(cand["regs"])
        shell = dict(strategy="v7", mode=cand["mode"], regs=cand["ends"])
        mut = _mutate_regs(rng, shell, space, mode, p_cont, p_menu, sigma)
        ends = mut["regs"]
        return dict(strategy="v7", mode=cand["mode"], cadapt=True, ends=ends,
                    regs=_interp_ends(ends[0], ends[1], R, space, mode))
    return _mutate_regs(rng, cand, space, mode, p_cont, p_menu, sigma)

def _mutate_regs(rng, cand, space, mode, p_cont=0.25, p_menu=0.10, sigma=0.10):
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
        for k, spec in space["flags"].items():
            fx = spec.get("fixed") if isinstance(spec, dict) else None
            if fx is not None:
                d[k] = float(fx)
            elif rng.random() < 0.05:
                d[k] = 1.0 - d[k]
        if mode == "spot":
            d["leverage"] = 1.0; d["eS3"] = 0.0; d["eXS"] = 0.0
        else:
            d["leverage"] = max(1.0, float(int(d["leverage"])))  # integer leverage (floor)
        out["regs"].append(d)
    return out

def build_P3(cand):
    P = np.vstack([vec3(reg) for reg in cand["regs"]])
    # SAFETY: clamp variant indices to the current grids. A candidate bred
    # from a different timeframe's pool can carry indices into the 1m-extended
    # grids; numba does NOT bounds-check, so an out-of-range index would read
    # garbage silently.
    from engine3 import VARIANTS as _V
    for col, g in ((45, "rsi"), (46, "macd"), (47, "bb"), (48, "ema"),
                   (49, "ema"), (50, "xmacd"), (51, "histn")):
        P[:, col] = np.clip(P[:, col], 0, len(_V[g]) - 1)
    return P

# ---------------- evaluation ----------------

def eval3(cand, method, t0=None, t1=None, warmup=3000, alt=None, gap_mode=None,
          scoring=None):
    G = load_g3()
    regs_list, R = G["regimes"][method]
    P = build_P3(cand)
    if P.shape[0] != R:  # allow single-set candidates on any method
        P = np.vstack([P[min(i, P.shape[0] - 1)] for i in range(R)])
    mode = cand["mode"]
    # OPT-IN lev stops (campaign c3 "survival"): stops active despite leverage.
    # The flag is stamped onto every evaluated candidate so saved configs
    # backtest/trade exactly as they were scored.
    if os.environ.get("LEV_STOPS") == "1" and mode == "lev":
        cand["lev_stops"] = True
    use_sl = (mode == "spot") or bool(cand.get("lev_stops"))
    comm = FUT_COMM if mode == "lev" else SPOT_COMM
    eq = 1000.0
    months = 0.0
    all_tr = []
    mtm_dd = 0.0
    liq_any = False
    max_hold = 0.0
    held_bars = 0.0
    total_bars = 0.0
    from wf2 import eval_intervals, contam_for
    for pre, reg in zip(G["pres"], regs_list):
        cm = contam_for(pre, warmup) if gap_mode == "skip_contaminated" else None
        t = pre["t"]
        i0 = 0 if t0 is None else int(np.searchsorted(t, np.datetime64(t0)))
        i1 = len(t) if t1 is None else int(np.searchsorted(t, np.datetime64(t1)))
        i0 = max(i0, warmup)
        if i1 - i0 < 200:
            continue
        ivs = eval_intervals(t, i0, i1, alt)
        for a, b in ivs:
            w0 = max(0, a - warmup)
            sp = slice_pre(pre, w0, b)
            eq_before = eq
            tr, eq, liq, op = run3(sp, P, regime=reg[w0:b], warmup=a - w0,
                                   initial_capital=eq, commission=comm,
                                   use_sl=use_sl, dyn_liq=(mode == "lev"),
                                   return_open=True,
                                   no_entry=(cm[w0:b] if cm is not None else None))
            total_bars += (b - a)
            if len(tr):
                max_hold = max(max_hold, float((tr["exit_idx"] - tr["entry_idx"]).max())
                               * _TFM / 1440.0)
                held_bars += float((tr["exit_idx"] - tr["entry_idx"]).sum())
            if op:
                op_held = len(sp["c"]) - 1 - op["entry_idx"]
                max_hold = max(max_hold, op_held * _TFM / 1440.0)
                held_bars += op_held
            months += (b - a) / (DAY * 30.4)
            all_tr.append(tr)
            if len(tr):
                _, dseg = mtm_curve(tr, sp["c"], initial=eq_before)
                mtm_dd = max(mtm_dd, dseg)
            if liq:
                liq_any = True
                break
        if liq_any:
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
    out = dict(n=len(tr), months=months, eq=float(eq), growth=float(growth),
               liq=liq_any, maxdd=float(mtm_dd), tpm=len(tr) / months,
               sl_hits=int((tr["reason"] == 1).sum()),
               worst_mae=float(tr["mae"].min()),
               win=float((tr["net"] > 0).mean()),
               max_hold_days=float(max_hold),
               in_market=float(held_bars / max(total_bars, 1.0)),
               # losing-period counts for the no-losing feasibility gates.
               # Weekly uses the same closed-trade equity the monthly series
               # does, so the gate matches what the Backtests page filter
               # ('no losing = month/week') measures.
               neg_months=int((gm < -1e-9).sum()),
               neg_weeks=int((pd.DataFrame(dict(
                   wk=pd.to_datetime(tr["exit_t"]).dt.to_period("W"), lg=lg))
                   .groupby("wk")["lg"].last().diff().dropna() < -1e-9).sum()),
               score=g_mean - 0.25 * g_std)
    if scoring == "worst_window":
        rw = gm.rolling(3).mean().dropna()
        out["score"] = float(rw.min()) if len(rw) else out["score"]
    elif scoring == "underwater":
        out["score"] = out["score"] - 0.5 * out["in_market"]
    elif scoring == "recent":
        # recency-weighted monthly growth (half-life 3 months): the newest
        # market character dominates the score, so configs must still work in
        # the LATEST era, not just the bull that opened the dataset
        if len(gm) >= 2:
            w = 0.5 ** (np.arange(len(gm))[::-1] / 3.0)
            out["score"] = float(np.average(gm.to_numpy(), weights=w)) - 0.25 * g_std
    for _k, _v in out.items():   # NaN/inf breaks JSON in browsers
        if isinstance(_v, float) and not np.isfinite(_v):
            out[_k] = 0.0
    return out

def feasible3(m, mode, min_tpm=None, min_n=10, cand=None, liq_margin=0.6, max_dd=None,
              max_hold=None):
    if m is None or m["liq"]:
        return False
    if max_hold and m.get("max_hold_days", 0.0) > max_hold:
        return False   # a position stayed open longer than allowed: throw the candidate out
    # env-default like wf2.feasible: worker processes never see the CLI args,
    # so --min-tpm travels as LAB_MIN_TPM in the inherited environment
    if min_tpm is None:
        min_tpm = float(os.environ.get("LAB_MIN_TPM", 2.0))
    if m["n"] < min_n or m["tpm"] < min_tpm:
        return False
    # no-losing-period gates — see wf2.feasible
    _mnm = os.environ.get("LAB_MAX_NEG_MONTHS")
    if _mnm not in (None, "") and m.get("neg_months", 0) > int(_mnm):
        return False
    _mnw = os.environ.get("LAB_MAX_NEG_WEEKS")
    if _mnw not in (None, "") and m.get("neg_weeks", 0) > int(_mnw):
        return False
    cap = max_dd if max_dd else (0.80 if mode == "lev" else 0.50)
    if m["maxdd"] > cap:
        return False
    if mode == "lev" and cand is not None:
        # Safety margin: the worst adverse excursion on train must clear the
        # liquidation distance with room to spare. Without this, long searches
        # converge on max leverage that "survived" training by a hair and
        # then liquidates on any unseen data.
        lev_max = max(r.get("leverage", 1.0) for r in cand.get("regs", [{}]))
        liq_dist = 1.0 / max(lev_max, 1e-9) - 0.008
        if m["worst_mae"] <= -liq_margin * liq_dist:
            return False
    return True

# ---------------- algorithms (single-process batch APIs) ----------------

def batch_random(rng, space, R, mode, method, n, t0, t1, per_regime=True, max_dd=None, alt=None, max_hold=None, gap_mode=None, scoring=None):
    out = []
    for _ in range(n):
        c = sample_candidate(rng, space, R, mode, per_regime)
        m = eval3(c, method, t0, t1, alt=alt, gap_mode=gap_mode, scoring=scoring)
        if feasible3(m, mode, cand=c, max_dd=max_dd, max_hold=max_hold):
            out.append((m["score"], c, m))
    return out

def batch_offspring(rng, space, mode, method, parents, n, t0, t1, max_dd=None, alt=None, max_hold=None, gap_mode=None, scoring=None):
    """Genetic step: produce and evaluate n children from a parent pool."""
    out = []
    for _ in range(n):
        if len(parents) >= 2:
            a, b = rng.choice(len(parents), 2, replace=False)
            child = crossover(rng, parents[a], parents[b])
        else:
            child = parents[0]
        child = mutate(rng, child, space, mode)
        m = eval3(child, method, t0, t1, alt=alt, gap_mode=gap_mode, scoring=scoring)
        if feasible3(m, mode, cand=child, max_dd=max_dd, max_hold=max_hold):
            out.append((m["score"], child, m))
    return out

def batch_refine(rng, space, mode, method, seed_cand, n, t0, t1, sigma=0.04, max_dd=None, alt=None, max_hold=None, gap_mode=None, scoring=None):
    out = []
    for _ in range(n):
        child = mutate(rng, seed_cand, space, mode, p_cont=0.15, p_menu=0.04, sigma=sigma)
        m = eval3(child, method, t0, t1, alt=alt, gap_mode=gap_mode, scoring=scoring)
        if feasible3(m, mode, cand=child, max_dd=max_dd, max_hold=max_hold):
            out.append((m["score"], child, m))
    return out
