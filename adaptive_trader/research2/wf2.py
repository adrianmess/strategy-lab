"""
Generic parallel walk-forward framework for both strategies.

Strategies:
  v6   : V5-family, volatility-normalized (z-scored MACD), optional cross-long
         re-enabled, optional trend-block on shorts.  (Tweaked => renamed V6.)
  scalpx: Scalp-family with regime-adaptive thresholds/branches. (=> ScalpX.)

Modes:
  lev  : futures, NO stop loss, leverage<=10, liquidation modeled per-trade
         (1/lev - 0.8% buffer). Constraint: never liquidated, maxDD<=80%.
  spot : long-only, 1x, stop-loss ON, spot fees. Constraint: maxDD<=50%.

Regime methods: none, vol3, vol3_7d, volume3, trend3, volXtrend9 (causal).
Window study: training window in {42d, 91d, 182d, 'all'}, refit every 28d.
"""
import numpy as np
import pandas as pd
import json, os, pickle, time

from engine import DEFAULT_PARAMS
from fast_engine import params_to_vec, run_fast, PARAM_NAMES
from scalp_engine import scalp_precompute, scalp_vec, run_scalp, SCALP_PARAM_NAMES
from scalp_engine import (scalp_precompute2, run_scalp2, slice_pre2,
                          SCALP2_VARIANTS, SCALP2_DEFAULT_IDX, scalp2_hash,
                          _SCALP2_DEFAULTS)
from adaptive import make_adaptive_pre, slice_pre
from regimes import regime_features, make_regimes, REGIME_METHODS, DAY
from common import load_segments

IDX = {k: i for i, k in enumerate(PARAM_NAMES)}
SIDX = {k: i for i, k in enumerate(SCALP_PARAM_NAMES)}

def mtm_curve(trades: pd.DataFrame, closes: np.ndarray, initial: float = 1000.0):
    """Mark-to-market equity per bar from trade list; returns (curve, maxdd)."""
    n = len(closes)
    eq_closed = initial
    mtm = np.empty(n)
    last = 0
    ei = trades["entry_idx"].to_numpy().astype(int)
    xi = trades["exit_idx"].to_numpy().astype(int)
    qty = trades["qty"].to_numpy()
    ent = trades["entry"].to_numpy()
    dr = trades["dir"].to_numpy()
    net = trades["net"].to_numpy()
    for k in range(len(ei)):
        i0, i1 = ei[k], min(xi[k] + 1, n)
        mtm[last:i0] = eq_closed
        mtm[i0:i1] = eq_closed + qty[k] * (closes[i0:i1] - ent[k]) * dr[k]
        eq_closed += net[k]
        last = i1
    mtm[last:] = eq_closed
    cummax = np.maximum.accumulate(mtm)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = (cummax - mtm) / np.maximum(cummax, 1e-12)
    return mtm, (float(np.nanmax(dd)) if n else 0.0)

RESULT_DIR = "wf2_results"
FUT_COMM = 0.0004
SPOT_COMM = 0.0005

REFIT_DATES = pd.date_range("2024-11-15", "2026-06-05", freq="28D")
TEST_DAYS = 28
TREND_VARIANTS = [None, 2.0]

# ---------------- global data (loaded once; fork-inherited) ----------------
def _cache_path(fn):
    """Pickle cache location. WF2_CACHE_DIR (set by the optimizer CLI) makes the
    heavy precompute caches shared across runs instead of per-run-dir."""
    d = os.environ.get("WF2_CACHE_DIR", "")
    if d:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, fn)
    return fn

_G = {}

def load_globals(need=("v6", "scalpx")):
    segs = None
    if "v6" in need and "v6" not in _G:
        v6 = None
        if os.path.exists(_cache_path("v6_variants.pkl")):
            try:
                v6 = pickle.load(open(_cache_path("v6_variants.pkl"), "rb"))
            except Exception as e:
                print(f"v6_variants.pkl unreadable ({e}); rebuilding...")
                try: os.remove(_cache_path("v6_variants.pkl"))
                except OSError: pass
        if v6 is None:
            segs = segs or load_segments()
            from fast_engine import precompute
            pres = [precompute(g, d1) for g, d1 in segs]
            v6 = []
            for tz in TREND_VARIANTS:
                pa = [make_adaptive_pre(p, trend_block_z=tz) for p in pres]
                v6.append(pa)  # list over segments of (q, f)
            pickle.dump(v6, open(_cache_path("v6_variants.pkl"), "wb"))
        _G["v6"] = v6
        _G["regimes_v6"] = {m: [make_regimes(f, m)[0] for _, f in v6[0]] for m in REGIME_METHODS}
        _G["nreg"] = {m: make_regimes(v6[0][0][1], m)[1] for m in REGIME_METHODS}
    if "scalpx2" in need and "scalp2" not in _G:
        sc2 = None
        cp = _cache_path(f"scalp2_pre_{scalp2_hash()}.pkl")
        _legacy = _cache_path("scalp2_pre.pkl")
        if not os.path.exists(cp) and os.path.exists(_legacy) \
                and SCALP2_VARIANTS == _SCALP2_DEFAULTS:
            try: os.rename(_legacy, cp)
            except OSError: pass
        if os.path.exists(cp):
            try:
                sc2 = pickle.load(open(cp, "rb"))
            except Exception as e:
                print(f"scalp2_pre.pkl unreadable ({e}); rebuilding...")
                try: os.remove(cp)
                except OSError: pass
        if sc2 is None:
            segs = segs or load_segments()
            sc2 = []
            for g, d1 in segs:
                pre = scalp_precompute2(g)
                f = regime_features(pre)
                sc2.append((pre, f))
            pickle.dump(sc2, open(cp, "wb"))
        _G["scalp2"] = sc2
        _G["regimes_sc2"] = {m: [make_regimes(f, m)[0] for _, f in sc2] for m in REGIME_METHODS}
        _G.setdefault("nreg", {m: make_regimes(sc2[0][1], m)[1] for m in REGIME_METHODS})
    if "scalpx" in need and "scalp" not in _G:
        sc = None
        if os.path.exists(_cache_path("scalp_pre.pkl")):
            try:
                sc = pickle.load(open(_cache_path("scalp_pre.pkl"), "rb"))
            except Exception as e:
                print(f"scalp_pre.pkl unreadable ({e}); rebuilding...")
                try: os.remove(_cache_path("scalp_pre.pkl"))
                except OSError: pass
        if sc is None:
            segs = segs or load_segments()
            sc = []
            for g, d1 in segs:
                pre = scalp_precompute(g)
                f = regime_features(pre)
                sc.append((pre, f))
            pickle.dump(sc, open(_cache_path("scalp_pre.pkl"), "wb"))
        _G["scalp"] = sc
        _G["regimes_sc"] = {m: [make_regimes(f, m)[0] for _, f in sc] for m in REGIME_METHODS}
        _G.setdefault("nreg", {m: make_regimes(sc[0][1], m)[1] for m in REGIME_METHODS})
    return _G

# ---------------- candidate samplers ----------------

def _per_regime(rng, base_lo, base_hi, R, jitter=0.35):
    base = rng.uniform(base_lo, base_hi)
    return np.clip(base * rng.uniform(1 - jitter, 1 + jitter, R),
                   min(base_lo, base_hi), max(base_lo, base_hi)).tolist()

def _rng_range(space, key, default):
    """Range for `key` from a param-space section, else the built-in default."""
    try:
        return tuple(space["continuous"][key]["range"])
    except Exception:
        return default

def sample_v6(rng, R, mode, space=None):
    s = space or {}
    c = dict(
        strategy="v6", tv=int(rng.integers(0, len(TREND_VARIANTS))),
        zL=_per_regime(rng, *_rng_range(s, "zL", (-2.6, -0.9)), R),
        zS=_per_regime(rng, *_rng_range(s, "zS", (0.9, 2.6)), R),
        zXS=_per_regime(rng, *_rng_range(s, "zXS", (1.1, 3.0)), R),
        zXLmax=(_per_regime(rng, *_rng_range(s, "zXLmax", (-2.0, 0.3)), R)
                if rng.random() < 0.5 else [-99.0] * R),
        ptScale=_per_regime(rng, *_rng_range(s, "ptScale", (0.5, 2.4)), R),
        eS3=[1.0] * R, eXS=[1.0] * R,
    )
    if mode == "lev":
        c["lev"] = _per_regime(rng, *_rng_range(s, "leverage", (1.2, 10.0)), R)
        c["sl"] = 0.0
        # occasionally disable short subsystems in some regimes
        if rng.random() < 0.35:
            c["eS3"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
        if rng.random() < 0.35:
            c["eXS"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
    else:  # spot: long only
        c["lev"] = [1.0] * R
        c["sl"] = float(rng.choice([0.02, 0.03, 0.04, 0.06, 0.08, 0.10]))
        c["eS3"] = [0.0] * R
        c["eXS"] = [0.0] * R
    return c

def sample_scalpx(rng, R, mode, space=None):
    s = space or {}
    c = dict(
        strategy="scalpx",
        tpL=_per_regime(rng, *_rng_range(s, "tpL", (0.002, 0.02)), R),
        tpS=_per_regime(rng, *_rng_range(s, "tpS", (0.002, 0.02)), R),
        rsiOB=_per_regime(rng, *_rng_range(s, "rsiOB", (50, 85)), R),
        rsiOS=_per_regime(rng, *_rng_range(s, "rsiOS", (15, 50)), R),
        useCvd=[1.0] * R, useEma=[1.0] * R,
        eL=[1.0] * R, eS=[1.0] * R,
    )
    if rng.random() < 0.4:
        c["useEma"] = rng.choice([0.0, 1.0], R, p=[0.4, 0.6]).tolist()
    if mode == "lev":
        c["lev"] = _per_regime(rng, *_rng_range(s, "leverage", (1.2, 10.0)), R)
        c["sl"] = 0.05
        c["slOn"] = 0.0
        if rng.random() < 0.4:
            c["eS"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
        if rng.random() < 0.4:
            c["eL"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
    else:
        c["lev"] = [1.0] * R
        sl_opts = (s.get("menus", {}).get("sl", {}) or {}).get("options") or [0.01, 0.02, 0.03, 0.05, 0.08]
        c["sl"] = float(rng.choice(sl_opts))
        c["slOn"] = 1.0
        c["eS"] = [0.0] * R
    return c

def sample_scalpx2(rng, R, mode, space=None):
    c = sample_scalpx(rng, R, mode, space)
    c["strategy"] = "scalpx2"
    menus = (space or {}).get("menus", {})
    for key, vkey in (("vR", "rsi"), ("vC", "cvd"), ("vP", "poc"), ("vE", "emaS")):
        opts = (menus.get(key, {}) or {}).get("options") \
            or list(range(len(SCALP2_VARIANTS[vkey])))
        c[key] = [float(rng.choice(opts)) for _ in range(R)]
    return c


# Solana Prime: the dip system standalone (no crossover). Base = the user's
# live parameters from their TradingView xlsx export (validated 105/111 trades).
PRIME_BASE = dict(DEFAULT_PARAMS)
PRIME_BASE.update(dict(
    rsiValLong=68.0, macdValPctLong=-0.235 / 100, bbValLong=-0.1,
    ptLong=1.21 / 100, slLong=0.10, apt1Long=0.7 / 100, apt2Long=0.35 / 100,
    dur1Long=0.6, dur2Long=43.2, cdPctLong=0.402 / 100, cdPeriodLong=90, cdTfLong="1",
    rsiValShort=68.0, macdValPctShort=0.21 / 100, bbValShort=1.65,
    ptShort=1.0 / 100, slShort=0.10, apt1Short=0.9 / 100, apt2Short=0.3 / 100,
    dur1Short=14.4, dur2Short=38.4, cdPctShort=0.31 / 100, cdPeriodShort=55, cdTfShort="3",
))

def sample_prime(rng, R, mode, space=None):
    s = space or {}
    g = lambda k, d: _per_regime(rng, *_rng_range(s, k, d), R)
    c = dict(
        strategy="prime", tv=int(rng.integers(0, len(TREND_VARIANTS))),
        zL=g("zL", (-2.8, -0.8)), zS=g("zS", (0.8, 2.8)),
        rsiL=g("rsiValLong", (30, 85)), rsiS=g("rsiValShort", (50, 90)),
        bbL=g("bbL", (-0.35, 0.25)), bbS=g("bbS", (0.6, 1.7)),
        ptL=g("ptLong", (0.002, 0.03)), a1L=g("apt1Long", (0.001, 0.02)),
        a2L=g("apt2Long", (0.0, 0.015)),
        d1L=g("dur1Long", (0.6, 90)), d2L=g("dur2Long", (10, 360)),
        ptS=g("ptShort", (0.002, 0.03)), a1S=g("apt1Short", (0.001, 0.02)),
        a2S=g("apt2Short", (0.0, 0.015)),
        d1S=g("dur1Short", (0.6, 90)), d2S=g("dur2Short", (10, 360)),
        cdPL=g("cdPctLong", (0.0005, 0.012)), cdTL=g("cdPeriodLong", (10, 240)),
        cdPS=g("cdPctShort", (0.0005, 0.012)), cdTS=g("cdPeriodShort", (10, 240)),
        eL3=[1.0] * R, eS3=[1.0] * R,
    )
    # orderings the strategy expects
    for r in range(R):
        if c["a1L"][r] > c["ptL"][r]: c["a1L"][r] = c["ptL"][r] * 0.7
        if c["a2L"][r] > c["a1L"][r]: c["a2L"][r] = c["a1L"][r] * 0.6
        if c["a1S"][r] > c["ptS"][r]: c["a1S"][r] = c["ptS"][r] * 0.7
        if c["a2S"][r] > c["a1S"][r]: c["a2S"][r] = c["a1S"][r] * 0.6
        if c["d2L"][r] < c["d1L"][r]: c["d1L"][r], c["d2L"][r] = c["d2L"][r], c["d1L"][r]
        if c["d2S"][r] < c["d1S"][r]: c["d1S"][r], c["d2S"][r] = c["d2S"][r], c["d1S"][r]
    if mode == "lev":
        c["lev"] = g("leverage", (1.2, 10.0))
        c["sl"] = 0.0
        if rng.random() < 0.25:
            c["eL3"] = rng.choice([0.0, 1.0], R, p=[0.2, 0.8]).tolist()
        if rng.random() < 0.35:
            c["eS3"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
    else:
        c["lev"] = [1.0] * R
        c["sl"] = float(rng.uniform(*_rng_range(s, "slLong", (0.02, 0.12))))
        c["eS3"] = [0.0] * R
    return c

def build_P_prime(c, R):
    rows = []
    for r in range(R):
        def pick(key, base_key=None, default=None):
            v = c.get(key)
            if v is not None:
                return v[r] if isinstance(v, list) else v
            if key == "ptScale":  # legacy candidates
                return None
            return PRIME_BASE[base_key] if base_key else default
        ov = dict(
            macdValPctLong=c["zL"][r], macdValPctShort=c["zS"][r],
            rsiValLong=pick("rsiL", "rsiValLong"), rsiValShort=pick("rsiS", "rsiValShort"),
            bbValLong=c["bbL"][r] if "bbL" in c else PRIME_BASE["bbValLong"],
            bbValShort=c["bbS"][r] if "bbS" in c else PRIME_BASE["bbValShort"],
            ptLong=pick("ptL", "ptLong"), apt1Long=pick("a1L", "apt1Long"),
            apt2Long=pick("a2L", "apt2Long"),
            dur1Long=pick("d1L", "dur1Long"), dur2Long=pick("d2L", "dur2Long"),
            ptShort=pick("ptS", "ptShort"), apt1Short=pick("a1S", "apt1Short"),
            apt2Short=pick("a2S", "apt2Short"),
            dur1Short=pick("d1S", "dur1Short"), dur2Short=pick("d2S", "dur2Short"),
            cdPctLong=pick("cdPL", "cdPctLong"), cdPeriodLong=pick("cdTL", "cdPeriodLong"),
            cdPctShort=pick("cdPS", "cdPctShort"), cdPeriodShort=pick("cdTS", "cdPeriodShort"),
            leverage=c["lev"][r],
            slLong=c["sl"] if c["sl"] > 0 else 0.10,
            slShort=c["sl"] if c["sl"] > 0 else 0.10,
            enableLong3m=(c.get("eL3", [1.0] * R))[r], enableShort3m=c["eS3"][r],
            enableLongX=0.0, enableShortX=0.0,
        )
        if "ptScale" in c:  # legacy candidate format (scale over live PTs)
            ps = c["ptScale"][r]
            for k in ["ptLong", "apt1Long", "apt2Long", "ptShort", "apt1Short", "apt2Short"]:
                ov[k] = PRIME_BASE[k] * ps
        rows.append(params_to_vec(PRIME_BASE, ov))
    return np.vstack(rows)


# ---------------- generic genome ops for flat candidates (v6/prime/scalpx) ----
FLAT_FLAG_KEYS = {"eL3", "eS3", "eXL", "eXS", "eL", "eS", "useCvd", "useEma"}
FLAT_MENU_KEYS = {"vR": "rsi", "vC": "cvd", "vP": "poc", "vE": "emaS"}  # scalpx2 variant indexes
FLAT_KEYMAP = {  # candidate key -> param-space key (for mutation ranges)
    "prime": dict(rsiL="rsiValLong", rsiS="rsiValShort", ptL="ptLong", a1L="apt1Long",
                  a2L="apt2Long", d1L="dur1Long", d2L="dur2Long", ptS="ptShort",
                  a1S="apt1Short", a2S="apt2Short", d1S="dur1Short", d2S="dur2Short",
                  cdPL="cdPctLong", cdTL="cdPeriodLong", cdPS="cdPctShort",
                  cdTS="cdPeriodShort", lev="leverage", bbL="bbL", bbS="bbS",
                  zL="zL", zS="zS"),
    "v6": dict(lev="leverage", zL="zL", zS="zS", zXS="zXS", zXLmax="zXLmax",
               ptScale="ptScale"),
    "scalpx": dict(lev="leverage", tpL="tpL", tpS="tpS", rsiOB="rsiOB", rsiOS="rsiOS"),
    "scalpx2": dict(lev="leverage", tpL="tpL", tpS="tpS", rsiOB="rsiOB", rsiOS="rsiOS"),
}

def _normalize_flat(c):
    """Re-impose per-strategy ordering constraints after crossover/mutation."""
    if c.get("strategy") == "prime":
        R = len(c["zL"])
        for r in range(R):
            if c["a1L"][r] > c["ptL"][r]: c["a1L"][r] = c["ptL"][r] * 0.7
            if c["a2L"][r] > c["a1L"][r]: c["a2L"][r] = c["a1L"][r] * 0.6
            if c["a1S"][r] > c["ptS"][r]: c["a1S"][r] = c["ptS"][r] * 0.7
            if c["a2S"][r] > c["a1S"][r]: c["a2S"][r] = c["a1S"][r] * 0.6
            if c["d2L"][r] < c["d1L"][r]: c["d1L"][r], c["d2L"][r] = c["d2L"][r], c["d1L"][r]
            if c["d2S"][r] < c["d1S"][r]: c["d1S"][r], c["d2S"][r] = c["d2S"][r], c["d1S"][r]
    return c

def crossover_flat(rng, a, b):
    child = {}
    for k in a:
        v = a[k] if rng.random() < 0.5 else b.get(k, a[k])
        child[k] = list(v) if isinstance(v, list) else v
    return _normalize_flat(child)

def mutate_flat(rng, cand, mode, space=None, p_cont=0.3, p_flag=0.05, sigma=0.10):
    s = space or {}
    strat = cand.get("strategy", "")
    keymap = FLAT_KEYMAP.get(strat, {})
    c = {k: (list(v) if isinstance(v, list) else v) for k, v in cand.items()}
    for k, v in c.items():
        if k in ("strategy",):
            continue
        if k == "tv":
            if rng.random() < 0.1:
                c[k] = int(rng.integers(0, len(TREND_VARIANTS)))
            continue
        if k == "sl":
            if mode == "spot" and rng.random() < p_cont:
                lo, hi = _rng_range(s, "slLong", (0.01, 0.12))
                c[k] = float(np.clip(v + rng.normal(0, sigma * (hi - lo)), lo, hi))
            continue
        if not isinstance(v, list):
            continue
        if k in FLAT_MENU_KEYS:
            menus = (s or {}).get("menus", {})
            opts = (menus.get(k, {}) or {}).get("options") \
                or list(range(len(SCALP2_VARIANTS[FLAT_MENU_KEYS[k]])))
            for r in range(len(v)):
                if rng.random() < 0.10:
                    v[r] = float(rng.choice(opts))
            continue
        if k in FLAT_FLAG_KEYS:
            for r in range(len(v)):
                if rng.random() < p_flag:
                    v[r] = 1.0 - v[r]
            continue
        rr = _rng_range(s, keymap.get(k, k), None) if s else None
        for r in range(len(v)):
            if rng.random() >= p_cont:
                continue
            # keep disabled-sentinels disabled (e.g. zXLmax = -99)
            if v[r] <= -90:
                continue
            if rr:
                lo, hi = rr
                v[r] = float(np.clip(v[r] + rng.normal(0, sigma * (hi - lo)), lo, hi))
            else:
                v[r] = float(v[r] + rng.normal(0, sigma * (abs(v[r]) + 1e-9)))
    return _normalize_flat(c)

def batch_offspring_flat(rng, parents, mode, space, sampler, R, method, n, t0, t1,
                         max_dd=None, alt=None, max_hold=None, gap_mode=None):
    """Genetic step for flat candidates; parents = list of cands."""
    out = []
    for _ in range(n):
        if len(parents) >= 2:
            i, j = rng.choice(len(parents), 2, replace=False)
            child = crossover_flat(rng, parents[i], parents[j])
        else:
            child = dict(parents[0])
        child = mutate_flat(rng, child, mode, space)
        m = eval_config(child, method, mode, t0, t1, alt=alt, gap_mode=gap_mode)
        if feasible(m, mode, cand=child, max_dd=max_dd, max_hold=max_hold):
            out.append((m["score"], child, m))
    return out

def batch_refine_flat(rng, seed_cand, mode, space, method, n, t0, t1, sigma=0.04,
                      max_dd=None, alt=None, max_hold=None, gap_mode=None):
    out = []
    for _ in range(n):
        child = mutate_flat(rng, seed_cand, mode, space, p_cont=0.15, sigma=sigma)
        m = eval_config(child, method, mode, t0, t1, alt=alt, gap_mode=gap_mode)
        if feasible(m, mode, cand=child, max_dd=max_dd, max_hold=max_hold):
            out.append((m["score"], child, m))
    return out

# ---------------- P-matrix builders ----------------

def build_P_v6(c, R):
    rows = []
    for r in range(R):
        ov = dict(
            macdValPctLong=c["zL"][r], macdValPctShort=c["zS"][r],
            xMacdMinShort=c["zXS"][r], xMacdMaxLong=c["zXLmax"][r],
            leverage=c["lev"][r],
            slLong=c["sl"] if c["sl"] > 0 else 0.10,
            slShort=c["sl"] if c["sl"] > 0 else 0.10,
            enableShort3m=c["eS3"][r], enableShortX=c["eXS"][r],
            enableLong3m=1.0,
            enableLongX=0.0 if c["zXLmax"][r] <= -90 else 1.0,
        )
        ps = c["ptScale"][r]
        for k in ["ptLong", "apt1Long", "apt2Long", "ptShort", "apt1Short", "apt2Short",
                  "xTpLong", "xApt1Long", "xApt2Long", "xTpShort", "xApt1Short", "xApt2Short"]:
            ov[k] = DEFAULT_PARAMS[k] * ps
        rows.append(params_to_vec(DEFAULT_PARAMS, ov))
    return np.vstack(rows)

def build_P_scalpx(c, R):
    rows = []
    for r in range(R):
        rows.append(scalp_vec(dict(
            tpLong=c["tpL"][r], tpShort=c["tpS"][r], sl=c["sl"],
            rsiOB=c["rsiOB"][r], rsiOS=c["rsiOS"][r], leverage=c["lev"][r],
            enableLong=c["eL"][r], enableShort=c["eS"][r],
            useCvdBranch=c["useCvd"][r], useEmaBranch=c["useEma"][r],
            slOn=c["slOn"] if "slOn" in c else 1.0)))
    return np.vstack(rows)

def build_P_scalpx2(c, R):
    P = build_P_scalpx(c, R)
    vidx = np.zeros((R, 4), dtype=np.int64)
    keys = ("rsi", "cvd", "poc", "emaS")
    for j, key in enumerate(("vR", "vC", "vP", "vE")):
        vals = c.get(key)
        for r in range(R):
            vidx[r, j] = int(vals[r]) if vals is not None else SCALP2_DEFAULT_IDX[keys[j]]
    return P, vidx

# ---------------- evaluation ----------------

def contamination_mask(t, warmup):
    """skip_contaminated: after each small intra-segment data gap (missing bars
    that did NOT trigger a segment split), suppress NEW entries while indicators
    re-converge: 30 bars per missing bar, min 60 bars (3h), max the full warmup.
    Exits/position management are unaffected."""
    t64 = t.astype("datetime64[m]")
    dt = np.diff(t64).astype(int)
    mask = np.zeros(len(t), dtype=np.int8)
    for j in np.where(dt > 4)[0]:
        missing = int(round(dt[j] / 3.0)) - 1
        W = int(np.clip(30 * missing, 60, warmup))
        mask[j + 1: j + 1 + W] = 1
    return mask


_CONTAM = {}

def contam_for(pre, warmup):
    """Memoized per-segment contamination mask (segments live for the process)."""
    key = (id(pre["t"]), warmup)
    m = _CONTAM.get(key)
    if m is None:
        m = contamination_mask(pre["t"], warmup)
        _CONTAM[key] = m
    return m


ALT_EPOCH = np.datetime64("2020-01-01")

def alt_intervals(t, i0, i1, days, part):
    """Alternating-block cross-validation: calendar blocks of `days` anchored at
    a fixed epoch. Even blocks -> train, odd blocks -> holdout. Returns the
    (a, b) index intervals inside [i0, i1) belonging to `part`."""
    if i1 - i0 < 2:
        return []
    step = np.timedelta64(int(days * 1440), "m")
    k0 = int((t[i0] - ALT_EPOCH) // step)
    k1 = int((t[i1 - 1] - ALT_EPOCH) // step)
    want = 0 if part == "train" else 1
    out = []
    for k in range(k0, k1 + 1):
        if k % 2 != want:
            continue
        a = max(int(np.searchsorted(t, ALT_EPOCH + k * step)), i0)
        b = min(int(np.searchsorted(t, ALT_EPOCH + (k + 1) * step)), i1)
        if b - a >= 200:
            out.append((a, b))
    return out


def eval_intervals(t, i0, i1, alt):
    """Index intervals to simulate, from the `alt` window spec:
      None                      -> the whole [i0, i1)
      (days, part)              -> alternating blocks (train=even / holdout=odd)
      dict(days, part, ranges)  -> restrict to `ranges` [(t0,t1) date strings,
                                   None = open side], then alternate if days.
    Used for train/holdout folds and lockboxes (cross-fit)."""
    if alt is None:
        return [(i0, i1)]
    if isinstance(alt, dict):
        days, part, ranges = alt.get("days"), alt.get("part"), alt.get("ranges")
    else:
        days, part, ranges = alt[0], alt[1], None
    windows = [(i0, i1)]
    if ranges:
        windows = []
        for r0, r1 in ranges:
            a = i0 if r0 is None else max(i0, int(np.searchsorted(t, np.datetime64(r0))))
            b = i1 if r1 is None else min(i1, int(np.searchsorted(t, np.datetime64(r1))))
            if b - a >= 200:
                windows.append((a, b))
    out = []
    for a, b in windows:
        if days:
            out.extend(alt_intervals(t, a, b, days, part))
        else:
            out.append((a, b))
    return out


def _clip_indices(t_arr, t0, t1):
    i0 = 0 if t0 is None else int(np.searchsorted(t_arr, np.datetime64(t0)))
    i1 = len(t_arr) if t1 is None else int(np.searchsorted(t_arr, np.datetime64(t1)))
    return i0, i1

def eval_config(cand, method, mode, t0, t1, collect_trades=False, alt=None,
                gap_mode=None):
    need = {"prime": ("v6",)}.get(cand["strategy"], (cand["strategy"],))
    G = load_globals(need)
    R = G["nreg"][method]
    warmup = 3000
    eq = 1000.0
    all_tr = []
    months = 0.0
    liq_any = False
    mtm_dd = 0.0
    max_hold = 0.0
    if cand["strategy"] in ("v6", "prime"):
        P = (build_P_prime if cand["strategy"] == "prime" else build_P_v6)(cand, R)
        segs = G["v6"][cand["tv"]]
        regs = G["regimes_v6"][method]
        comm = FUT_COMM
        use_sl = (mode == "spot")
        for (pre, f), reg in zip(segs, regs):
            i0, i1 = _clip_indices(pre["t"], t0, t1)
            i0 = max(i0, warmup)   # never trade unconverged indicators
            if i1 - i0 < 200: continue
            cm = contam_for(pre, warmup) if gap_mode == "skip_contaminated" else None
            ivs = eval_intervals(pre["t"], i0, i1, alt)
            for a, b in ivs:
                w0 = max(0, a - warmup)
                sp = slice_pre(pre, w0, b)
                eq_before = eq
                tr, eq, liq, op = run_fast(sp, P, regime=reg[w0:b], warmup=a - w0,
                                           initial_capital=eq, use_sl=use_sl,
                                           commission=comm if mode == "lev" else SPOT_COMM,
                                           liq_threshold=-1.0 if mode == "lev" else 1e9,
                                           return_open=True,
                                           no_entry=(cm[w0:b] if cm is not None else None))
                if len(tr):
                    max_hold = max(max_hold, float((tr["exit_idx"] - tr["entry_idx"]).max())
                                   * 3.0 / 1440.0)
                if op:
                    max_hold = max(max_hold,
                                   (len(sp["c"]) - 1 - op["entry_idx"]) * 3.0 / 1440.0)
                months += (b - a) / (DAY * 30.4)
                all_tr.append(tr)
                if mode == "lev" and len(tr):
                    _, dseg = mtm_curve(tr, sp["c"], initial=eq_before)
                    mtm_dd = max(mtm_dd, dseg)
                if liq: liq_any = True; break
            if liq_any: break
    else:
        sx2 = cand["strategy"] == "scalpx2"
        if sx2:
            P, vidx = build_P_scalpx2(cand, R)
            segs_sc, regs_sc = G["scalp2"], G["regimes_sc2"][method]
        else:
            P = build_P_scalpx(cand, R)
            segs_sc, regs_sc = G["scalp"], G["regimes_sc"][method]
        comm = FUT_COMM if mode == "lev" else SPOT_COMM
        warmup = 2500
        for (pre, f), reg in zip(segs_sc, regs_sc):
            i0, i1 = _clip_indices(pre["t"], t0, t1)
            i0 = max(i0, warmup)   # never trade unconverged indicators
            if i1 - i0 < 200: continue
            cm = contam_for(pre, warmup) if gap_mode == "skip_contaminated" else None
            ivs = eval_intervals(pre["t"], i0, i1, alt)
            for a, b in ivs:
                w0 = max(0, a - warmup)
                sp = slice_pre2(pre, w0, b) if sx2 else slice_pre(pre, w0, b)
                eq_before = eq
                ne = cm[w0:b] if cm is not None else None
                if sx2:
                    tr, eq, liq, op = run_scalp2(sp, P, vidx, regime=reg[w0:b], warmup=a - w0,
                                                 initial_capital=eq, commission=comm,
                                                 liq_threshold=-1.0 if mode == "lev" else 1e9,
                                                 return_open=True, no_entry=ne)
                else:
                    tr, eq, liq, op = run_scalp(sp, P, regime=reg[w0:b], warmup=a - w0,
                                                initial_capital=eq, commission=comm,
                                                liq_threshold=-1.0 if mode == "lev" else 1e9,
                                                return_open=True, no_entry=ne)
                if len(tr):
                    max_hold = max(max_hold, float((tr["exit_idx"] - tr["entry_idx"]).max())
                                   * 3.0 / 1440.0)
                if op:
                    max_hold = max(max_hold,
                                   (len(sp["c"]) - 1 - op["entry_idx"]) * 3.0 / 1440.0)
                months += (b - a) / (DAY * 30.4)
                all_tr.append(tr)
                if mode == "lev" and len(tr):
                    _, dseg = mtm_curve(tr, sp["c"], initial=eq_before)
                    mtm_dd = max(mtm_dd, dseg)
                if liq: liq_any = True; break
            if liq_any: break
    if months <= 0:
        return None
    tr = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    if len(tr) == 0:
        return None
    e = tr["net"].cumsum() + 1000.0
    dd = float(((e.cummax() - e) / e.cummax()).max())
    if mode == "lev":
        dd = max(dd, mtm_dd)
    growth = np.log(max(eq, 1e-9) / 1000.0) / months
    mo = pd.to_datetime(tr["exit_t"]).dt.to_period("M")
    lg = np.log(np.maximum(e.to_numpy(), 1e-9))
    gm = pd.DataFrame(dict(mo=mo, lg=lg)).groupby("mo")["lg"].last().diff().dropna()
    g_mean = float(gm.mean()) if len(gm) >= 2 else growth
    g_std = float(gm.std()) if len(gm) >= 2 else 0.0
    out = dict(n=len(tr), months=months, eq=eq, growth=growth, liq=liq_any,
               maxdd=dd, tpm=len(tr) / months,
               sl_hits=int((tr["reason"] == 1).sum()),
               worst_mae=float(tr["mae"].min()),
               win=float((tr["net"] > 0).mean()),
               max_hold_days=float(max_hold),
               score=g_mean - 0.25 * g_std)
    for k, v in out.items():   # NaN/inf breaks JSON in browsers
        if isinstance(v, float) and not np.isfinite(v):
            out[k] = 0.0
    if collect_trades:
        out["trades"] = tr
    return out

def feasible(m, mode, cand=None, max_dd=None, liq_margin=0.6, max_hold=None):
    if m is None or m["liq"]:
        return False
    if max_hold and m.get("max_hold_days", 0.0) > max_hold:
        return False   # a position stayed open longer than allowed: throw the candidate out
    if m["n"] < 10 or m["tpm"] < 2:
        return False
    cap = max_dd if max_dd else (0.80 if mode == "lev" else 0.50)
    if m["maxdd"] > cap:
        return False
    if mode == "lev" and cand is not None and "lev" in cand:
        # Safety margin: worst adverse excursion on train must clear the
        # liquidation distance with room to spare, else long searches converge
        # on max-leverage configs that survived training by a hair and then
        # liquidate on unseen data (observed on V7 before this gate existed).
        lev_max = max(cand["lev"]) if isinstance(cand["lev"], list) else cand["lev"]
        liq_dist = 1.0 / max(lev_max, 1e-9) - 0.008
        if m["worst_mae"] <= -liq_margin * liq_dist:
            return False
    return True

def average_cands(cands):
    out = dict(strategy=cands[0]["strategy"])
    keys = [k for k in cands[0] if k not in ("strategy",)]
    for k in keys:
        vals = [c[k] for c in cands]
        if isinstance(vals[0], list):
            arr = np.mean([v for v in vals], axis=0)
            # binary flags: majority vote
            if k in ("eS3", "eXS", "eL", "eS", "useCvd", "useEma"):
                arr = (arr >= 0.5).astype(float)
            out[k] = np.round(arr, 4).tolist()
        elif k == "tv":
            from collections import Counter
            out[k] = Counter(vals).most_common(1)[0][0]
        elif k == "slOn":
            out[k] = vals[0]
        else:
            out[k] = float(np.median(vals))
    # keep cross-long disabled if majority disabled
    if "zXLmax" in out:
        n_off = sum(1 for c in cands if c["zXLmax"][0] <= -90)
        if n_off > len(cands) / 2:
            out["zXLmax"] = [-99.0] * len(out["zXLmax"])
    return out

# ---------------- fold job ----------------

def run_fold(job):
    """job: dict(strategy, mode, method, window, fold_idx, n_samples, seed)"""
    t_test0 = REFIT_DATES[job["fold_idx"]]
    t_test1 = t_test0 + pd.Timedelta(days=TEST_DAYS)
    if job["window"] == "all":
        t_train0 = None
    else:
        t_train0 = t_test0 - pd.Timedelta(days=int(job["window"]))
    G = load_globals(("v6",) if job["strategy"] == "prime" else (job["strategy"],))
    R = G["nreg"][job["method"]]
    rng = np.random.default_rng(job["seed"] + job["fold_idx"] * 7919)
    sampler = {"v6": sample_v6, "prime": sample_prime}.get(job["strategy"], sample_scalpx)
    feas = []
    for _ in range(job["n_samples"]):
        c = sampler(rng, R, job["mode"])
        m = eval_config(c, job["method"], job["mode"], t_train0, t_test0)
        if feasible(m, job["mode"]):
            feas.append((m["score"], c, m))
    if not feas:
        return dict(job=job, status="no_feasible")
    feas.sort(key=lambda x: -x[0])
    top = [c for _, c, _ in feas[:5]]
    avg = average_cands(top)
    m_avg = eval_config(avg, job["method"], job["mode"], t_train0, t_test0)
    if feasible(m_avg, job["mode"]):
        chosen, chosen_m = avg, m_avg
    else:
        chosen, chosen_m = feas[0][1], feas[0][2]
    oos = eval_config(chosen, job["method"], job["mode"],
                      str(t_test0.date()), str(t_test1.date()))
    return dict(job=job, status="ok", n_feasible=len(feas), cand=chosen,
                train={k: v for k, v in chosen_m.items() if k != "trades"},
                oos=None if oos is None else {k: v for k, v in oos.items() if k != "trades"})
