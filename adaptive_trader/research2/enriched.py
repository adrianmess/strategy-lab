"""Enriched features for the research engines: order book (ob), true CVD
(cvd), tick volume profile (vp) and open interest (oi), plus MEXC funding
as a cost. Built by research/enriched_features.py into per-bar parquet
files aligned to the candle files; this module is the ONLY place the
engines touch them.

HOW IT PLUGS IN (all opt-in, classic runs are byte-identical)
    LAB_FEATURES=ob,cvd,vp,oi   which features gate entries this run
    LAB_FUNDING=1               charge MEXC funding at each 8h settlement

  * attach_*()     after the engine caches load, add f_* arrays to every
                   segment 'pre' (never cached — the pickles stay valid)
  * inject_space() add the gate parameters to the search space, so the
                   existing samplers / mutation / crossover handle them
                   like any other per-regime parameter
  * gate_kwargs()  per evaluation: no_long / no_short masks from the
                   candidate's gate thresholds and the features — the cores
                   consume them exactly like the gap-contamination mask
  * charge_funding() per evaluation: subtract funding from each trade's net

GATES (all per-regime, like every other parameter)
    ob   gObMin  [0, 0.6]    book imbalance (bid-ask)/(bid+ask) at +-gObLvl%
                            must be >= gObMin for a long, <= -gObMin for a short
         gObLvl  {1,2,5}
    cvd  gCvdZ   [0, 2]      z-score of CVD change over gCvdLen bars
                            (vs its own 5x window) >= gCvdZ long / <= -gCvdZ short
         gCvdLen {20,60,120,300}
    vp   gVpDist [0, 1.5%]   close must sit >= gVpDist above the gVpLook-minute
                            POC for a long, below it for a short
         gVpLook {120,300,600}
    oi   gOiChg  [0, 8%]     gOiDir * (OI change over gOiWin x 5min) >= gOiChg
         gOiWin  {6,12,48}   for either direction (rising OI confirms; -1 = unwind)
         gOiDir  {-1, +1}
A bar where any enabled feature is missing blocks BOTH directions: a run
outside the feature history is refused at the bar level, never zero-filled.
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FEAT_DIR = os.path.join(HERE, "..", "research", "features")

FEATURES = ("ob", "cvd", "vp", "oi")
OB_LVLS = (1, 2, 5)
CVD_LENS = (20, 60, 120, 300)
VP_LOOKS = (120, 300, 600)
OI_WINS5 = (6, 12, 48)          # in 5-minute units (the metrics cadence)

GATE_SPECS = {
    "ob":  {"continuous": {"gObMin": (0.0, 0.6)},   "menus": {"gObLvl": list(OB_LVLS)}},
    "cvd": {"continuous": {"gCvdZ": (0.0, 2.0)},    "menus": {"gCvdLen": list(CVD_LENS)}},
    "vp":  {"continuous": {"gVpDist": (0.0, 0.015)}, "menus": {"gVpLook": list(VP_LOOKS)}},
    "oi":  {"continuous": {"gOiChg": (0.0, 0.08)},  "menus": {"gOiWin": list(OI_WINS5),
                                                                "gOiDir": [-1.0, 1.0]}},
}
GATE_KEYS = tuple(k for f in GATE_SPECS.values() for k in list(f["continuous"]) + list(f["menus"]))
GATE_MENU_KEYS = tuple(k for f in GATE_SPECS.values() for k in f["menus"])
RANGES = {k: v for f in GATE_SPECS.values() for k, v in f["continuous"].items()}


# ---------------------------------------------------------------- switches --
def enabled():
    v = (os.environ.get("LAB_FEATURES") or "").strip().lower()
    if not v:
        return set()
    return {x.strip() for x in v.split(",") if x.strip() in FEATURES}


def funding_on():
    return os.environ.get("LAB_FUNDING") == "1"


def active():
    return bool(enabled()) or funding_on()


def feature_tag(feats=None):
    """Canonical suffix for run names: '+ob+cvd' in fixed order."""
    f = enabled() if feats is None else set(feats)
    return "".join("+" + x for x in FEATURES if x in f)


# -------------------------------------------------------------- the space --
def inject_space(space, feats=None):
    """Return a copy of `space` with the gate parameters for the enabled
    features added (continuous ranges + menus) in the param_space.json
    shape, so every sampler / mutator treats them as ordinary parameters."""
    feats = enabled() if feats is None else set(feats)
    if not feats:
        return space
    s = dict(space or {})
    s["continuous"] = dict(s.get("continuous") or {})
    s["menus"] = dict(s.get("menus") or {})
    for f in FEATURES:
        if f not in feats:
            continue
        for k, (lo, hi) in GATE_SPECS[f]["continuous"].items():
            s["continuous"].setdefault(k, {"range": [lo, hi], "label": k,
                                           "doc": f"enriched gate ({f})", "order": 900})
        for k, opts in GATE_SPECS[f]["menus"].items():
            s["menus"].setdefault(k, {"options": list(map(float, opts)),
                                      "labels": [str(o) for o in opts]})
    return s


def ensure_gates(cand, rng, R, feats=None):
    """Flat-family candidates: add per-regime gate lists for the enabled
    features when missing (random samples are drawn by the family's own
    sampler, which knows nothing about gates). v7 candidates get theirs
    from the injected space directly."""
    feats = enabled() if feats is None else set(feats)
    if not feats or "regs" in cand:
        return cand
    for f in FEATURES:
        if f not in feats:
            continue
        for k, (lo, hi) in GATE_SPECS[f]["continuous"].items():
            if k not in cand:
                cand[k] = [float(x) for x in rng.uniform(lo, hi, R)]
        for k, opts in GATE_SPECS[f]["menus"].items():
            if k not in cand:
                cand[k] = [float(rng.choice(opts)) for _ in range(R)]
    return cand


# --------------------------------------------------------------- features --
_FRAMES = {}


def _coin_tf():
    coin = (os.environ.get("LAB_COIN") or "sol").lower()
    tf = int(os.environ.get("LAB_TF") or 3)
    return coin, tf


def load_frame(coin=None, tf=None):
    """The aligned per-bar feature parquet for (coin, tf), indexed by naive
    UTC bar start (matches pre['t']). Cached per process."""
    if coin is None or tf is None:
        coin, tf = _coin_tf()
    key = (coin, tf)
    if key in _FRAMES:
        return _FRAMES[key]
    p = os.path.join(FEAT_DIR, f"{coin}_{tf}min.parquet")
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"enriched features not built for {coin} {tf}m — run "
            f"research/enriched_features.py build --coin {coin} (expected {p})")
    df = pd.read_parquet(p)
    t = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None).values.astype("datetime64[ns]")
    df = df.drop(columns=["t"])
    df.index = t
    _FRAMES[key] = df
    return df


def coverage(coin=None, tf=None):
    """(first, last, fraction) of bars where every feature input is present."""
    df = load_frame(coin, tf)
    ok = df["obi1"].notna() & df["cvd_delta"].notna() & df["oi"].notna() & df["poc300"].notna()
    if not ok.any():
        return None, None, 0.0
    return df.index[ok].min(), df.index[ok].max(), float(ok.mean())


def _rolling_z(x, L):
    s = pd.Series(x)
    d = s - s.shift(L)
    w = max(5 * L, 50)
    m = d.rolling(w, min_periods=max(L, 20)).mean()
    sd = d.rolling(w, min_periods=max(L, 20)).std()
    return ((d - m) / sd.replace(0, np.nan)).values


def attach(pre, frame=None, feats=None, funding=None):
    """Add f_* arrays (aligned to pre['t']) to one segment pre dict. Idempotent."""
    feats = enabled() if feats is None else set(feats)
    funding = funding_on() if funding is None else funding
    if not feats and not funding:
        return pre
    if pre.get("_enriched") == (tuple(sorted(feats)), funding):
        return pre
    df = load_frame() if frame is None else frame
    t = np.asarray(pre["t"]).astype("datetime64[ns]")
    n = len(t)
    idx = df.index.get_indexer(t)          # -1 where the candle has no feature row
    has = idx >= 0

    def col(name):
        out = np.full(n, np.nan)
        if name in df.columns:
            out[has] = df[name].values[idx[has]]
        return out

    ok = np.ones(n, dtype=bool)
    c = np.asarray(pre["c"], dtype=np.float64)
    if "ob" in feats:
        pre["f_obi"] = np.vstack([col(f"obi{L}") for L in OB_LVLS])
        ok &= np.isfinite(pre["f_obi"]).all(axis=0)
    if "cvd" in feats:
        cvd = col("cvd")
        pre["f_cvdz"] = np.vstack([_rolling_z(cvd, L) for L in CVD_LENS])
        ok &= np.isfinite(cvd)
    if "vp" in feats:
        pocs = [col(f"poc{L}") for L in VP_LOOKS]
        pre["f_vpd"] = np.vstack([(c - p) / np.where(c > 0, c, np.nan) for p in pocs])
        pre["f_poc"] = np.vstack(pocs)           # raw POC prices (new families)
        pre["f_vah"] = col("vah"); pre["f_val"] = col("val")
        ok &= np.isfinite(np.vstack(pocs)).all(axis=0)
    if "oi" in feats:
        oi = col("oi")
        _, tf = _coin_tf()
        rows = []
        for w5 in OI_WINS5:
            w = max(1, int(round(w5 * 5 / tf)))
            prev = np.full(n, np.nan)
            prev[w:] = oi[:-w]
            rows.append(oi / np.where(prev > 0, prev, np.nan) - 1.0)
        pre["f_oic"] = np.vstack(rows)
        ok &= np.isfinite(oi)
    pre["f_ok"] = ok.astype(np.int8)
    if funding:
        fr = col("fund_rate"); fs = col("fund_settle")
        f = np.where(np.isfinite(fr) & np.isfinite(fs), fr * fs, 0.0)
        pre["f_fund"] = f
    pre["_enriched"] = (tuple(sorted(feats)), funding)
    return pre


def attach_globals(G):
    """Walk wf2's _G structure and attach to every segment pre."""
    if not active():
        return G
    df = load_frame()
    for segs in (G.get("v6") or []):        # list over trend variants
        for q, _f in segs:                   # list over segments of (q, f)
            attach(q, df)
    for key in ("macdx", "rocx"):
        for pre in G.get(key) or []:
            attach(pre, df)
    for key in ("scalp2", "scalp"):
        for q, _f in G.get(key) or []:
            attach(q, df)
    return G


def attach_pres(pres):
    """engine3 / v7: a plain list of segment pre dicts."""
    if not active():
        return pres
    df = load_frame()
    for pre in pres:
        attach(pre, df)
    return pres


# ----------------------------------------------------------------- masks ----
def _per_regime(cand, key, R):
    if "regs" in cand:                       # v7 shape
        vals = [reg.get(key) for reg in cand["regs"]]
        if any(v is None for v in vals):
            return None
        return np.asarray(vals, dtype=np.float64)
    v = cand.get(key)
    if v is None:
        return None
    if isinstance(v, (list, tuple, np.ndarray)):
        a = np.asarray(v, dtype=np.float64)
        return a if len(a) == R else np.resize(a, R)
    return np.full(R, float(v))


def _menu_idx(vals, options):
    opts = np.asarray(options, dtype=np.float64)
    return np.array([int(np.argmin(np.abs(opts - x))) for x in vals], dtype=np.int64)


def gate_kwargs(sp, reg, cand):
    """no_long / no_short int8 masks for this slice, or {} when no feature
    gates apply (classic run, or a candidate without gate keys)."""
    feats = enabled()
    if not feats or "f_ok" not in sp:
        return {}
    n = len(sp["c"])
    reg = np.asarray(reg, dtype=np.int64)
    R = int(reg.max()) + 1 if len(reg) else 1
    ar = np.arange(n)
    nl = sp["f_ok"] == 0
    ns = nl.copy()
    used = False
    if "ob" in feats and "f_obi" in sp:
        thr = _per_regime(cand, "gObMin", R); lvl = _per_regime(cand, "gObLvl", R)
        if thr is not None:
            li = _menu_idx(lvl if lvl is not None else np.full(R, 2.0), OB_LVLS)
            x = sp["f_obi"][li[reg], ar]; th = thr[reg]
            nl |= ~(x >= th); ns |= ~(x <= -th); used = True
    if "cvd" in feats and "f_cvdz" in sp:
        thr = _per_regime(cand, "gCvdZ", R); ln = _per_regime(cand, "gCvdLen", R)
        if thr is not None:
            li = _menu_idx(ln if ln is not None else np.full(R, 60.0), CVD_LENS)
            x = sp["f_cvdz"][li[reg], ar]; th = thr[reg]
            nl |= ~(x >= th); ns |= ~(x <= -th); used = True
    if "vp" in feats and "f_vpd" in sp:
        thr = _per_regime(cand, "gVpDist", R); lk = _per_regime(cand, "gVpLook", R)
        if thr is not None:
            li = _menu_idx(lk if lk is not None else np.full(R, 300.0), VP_LOOKS)
            x = sp["f_vpd"][li[reg], ar]; th = thr[reg]
            nl |= ~(x >= th); ns |= ~(x <= -th); used = True
    if "oi" in feats and "f_oic" in sp:
        thr = _per_regime(cand, "gOiChg", R); wn = _per_regime(cand, "gOiWin", R)
        dr = _per_regime(cand, "gOiDir", R)
        if thr is not None:
            li = _menu_idx(wn if wn is not None else np.full(R, 12.0), OI_WINS5)
            d = (dr if dr is not None else np.ones(R))[reg]
            x = sp["f_oic"][li[reg], ar] * d; th = thr[reg]
            blk = ~(x >= th)
            nl |= blk; ns |= blk; used = True
    if not used and not (sp["f_ok"] == 0).any():
        return {}
    return dict(no_long=nl.astype(np.int8), no_short=ns.astype(np.int8))


# --------------------------------------------------------------- funding ----
def charge_funding(tr, sp, mode):
    """Subtract MEXC funding from each trade's net: sum over the settlements
    inside (entry, exit] of rate x dir x qty x close. Longs pay a positive
    rate. Returns the total charged (so callers keep the equity chain
    consistent). Perps only; no-op unless LAB_FUNDING=1."""
    if not funding_on() or mode != "lev" or tr is None or len(tr) == 0 or "f_fund" not in sp:
        return 0.0
    f = np.asarray(sp["f_fund"], dtype=np.float64)
    if not f.any():
        if "funding" not in tr:
            tr["funding"] = 0.0
        return 0.0
    c = np.asarray(sp["c"], dtype=np.float64)
    F = np.concatenate([[0.0], np.cumsum(f * c)])
    ei = tr["entry_idx"].values.astype(np.int64)
    xi = tr["exit_idx"].values.astype(np.int64)
    d = tr["dir"].values.astype(np.float64)
    q = tr["qty"].values.astype(np.float64)
    cost = d * q * (F[np.clip(xi + 1, 0, len(F) - 1)] - F[np.clip(ei + 1, 0, len(F) - 1)])
    tr["funding"] = cost
    tr["net"] = tr["net"].values - cost
    return float(cost.sum())
