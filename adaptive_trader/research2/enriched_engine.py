"""Feature-native strategy families ("enriched" families).

Four families that trade the Binance-derived microstructure features
directly (order-book imbalance, CVD z-score, volume-profile POC/value area,
open-interest change) instead of price indicators. They share ONE numba
core with a common position machine (next-bar-open fills, TP / SL / time
stop / structure exit, reversal, liquidation, cooldown) and differ only in
the entry and structure-exit rules:

  flowx      order-flow momentum: CVD z crosses above zIn with the book
             leaning the same way (obi >= obMin). Structure exit when the
             flow flips (z <= -zOut). Symmetric shorts.
  poctrend   volume-profile breakout: close holds beyond POC by brk for
             cfm consecutive bars (optionally with CVD z > 0 agreeing);
             structure exit when price falls back through POC by fail.
  oisqueeze  open-interest squeeze: OI up >= oiThr over the window while
             price moved AGAINST the new positioning (down => new shorts),
             and price then breaks the brkN-bar high => long squeeze.
             Mirror for shorts. Structure exit when the short-window OI
             change <= -oiOut (the crowd is unwinding).
  absorb     absorption / exhaustion: heavy one-sided flow (z <= -zAbs)
             that fails to print a new nLow-bar low, below POC by dist,
             with bid support (obi >= obMin) => long mean reversion to POC
             (structure exit when close reaches POC). Mirror for shorts.

Requirements: the segment pre must carry the enriched arrays that
research2/enriched.attach() adds (f_obi, f_cvdz, f_vpd, f_poc, f_vah,
f_val, f_oic, f_ok) — i.e. LAB_FEATURES=ob,cvd,vp,oi. Bars without full
feature coverage never enter.

P-matrix layout (per-regime rows) — ENR_PNAMES:
  0 tpL  1 tpS  2 slL  3 slS  4 minT(min)  5 maxH(hours, 0=off)  6 lev
  7 eL  8 eS  9 p1  10 p2  11 p3  12 p4  13 v1  14 v2  15 f1
with the family-specific meaning of p1..p4 / v1 / v2 / f1 in FAMILIES.
"""
import os

import numpy as np
import pandas as pd
from numba import njit

from enriched import OB_LVLS, CVD_LENS, VP_LOOKS, OI_WINS5

ENR_FAMILIES = ("flowx", "poctrend", "oisqueeze", "absorb")
FAM_ID = {f: i for i, f in enumerate(ENR_FAMILIES)}

ENR_PNAMES = ["tpL", "tpS", "slL", "slS", "minT", "maxH", "lev", "eL", "eS",
              "p1", "p2", "p3", "p4", "v1", "v2", "f1"]

# candidate keys per family: (key, kind, default range/options, doc)
#   kind: "c" continuous, "i" integer (rounded), "m" menu (options list),
#         "f" flag
FAMILIES = {
    "flowx": dict(
        p1=("zIn", "c", (0.3, 2.5), "CVD z-score that must be crossed to enter"),
        p2=("zOut", "c", (0.0, 2.0), "structure exit when flow flips past -zOut"),
        p3=("obMin", "c", (0.0, 0.5), "min book imbalance agreeing with the trade"),
        p4=("pad", "c", (0.0, 0.0), "unused"),
        v1=("vCvdE", "m", list(range(len(CVD_LENS))), "CVD z lookback (20/60/120/300)"),
        v2=("vObE", "m", list(range(len(OB_LVLS))), "book depth level (1/2/5%)"),
        f1=("f1", "f", None, "unused"),
    ),
    "poctrend": dict(
        p1=("brk", "c", (0.0, 0.01), "close must clear POC by this fraction"),
        p2=("fail", "c", (0.0, 0.01), "structure exit when back through POC by this"),
        p3=("cfm", "i", (1, 10), "consecutive bars beyond POC before entering"),
        p4=("pad", "c", (0.0, 0.0), "unused"),
        v1=("vVpE", "m", list(range(len(VP_LOOKS))), "POC lookback (120/300/600 min)"),
        v2=("vCvdE", "m", list(range(len(CVD_LENS))), "CVD z lookback for the flow filter"),
        f1=("useFlow", "f", None, "require CVD z agreeing with the breakout"),
    ),
    "oisqueeze": dict(
        p1=("oiThr", "c", (0.002, 0.05), "OI change over the window that marks crowding"),
        p2=("pxThr", "c", (0.0, 0.02), "price move against the crowd over the window"),
        p3=("brkN", "i", (3, 48), "bars of high/low the squeeze must break"),
        p4=("oiOut", "c", (0.0, 0.05), "structure exit when 30-min OI change <= -oiOut"),
        v1=("vOiE", "m", list(range(len(OI_WINS5))), "OI window (30m / 1h / 4h)"),
        v2=("vObE", "m", list(range(len(OB_LVLS))), "unused"),
        f1=("f1", "f", None, "unused"),
    ),
    "absorb": dict(
        p1=("zAbs", "c", (0.5, 3.0), "one-sided flow z that must be absorbed"),
        p2=("nLow", "i", (3, 30), "no new low/high over this many bars"),
        p3=("obMin", "c", (0.0, 0.5), "book must lean toward the reversal"),
        p4=("dist", "c", (0.0, 0.01), "price must be beyond POC by this fraction"),
        v1=("vCvdE", "m", list(range(len(CVD_LENS))), "CVD z lookback"),
        v2=("vObE", "m", list(range(len(OB_LVLS))), "book depth level"),
        f1=("f1", "f", None, "unused"),
    ),
}
COMMON = dict(
    tpL=("c", (0.003, 0.03), "take profit, longs (fraction)"),
    tpS=("c", (0.003, 0.03), "take profit, shorts"),
    slL=("c", (0.004, 0.04), "stop loss, longs (0 = off)"),
    slS=("c", (0.004, 0.04), "stop loss, shorts"),
    minT=("c", (1.0, 45.0), "cooldown between orders (minutes)"),
    maxH=("c", (0.0, 72.0), "time stop (hours, 0 = off)"),
)
ENR_MENU_KEYS = ("vCvdE", "vObE", "vVpE", "vOiE")
ENR_INT_KEYS = ("cfm", "brkN", "nLow")
ENR_FLAG_KEYS = ("useFlow",)
MENU_OPTIONS = {"vCvdE": list(range(len(CVD_LENS))), "vObE": list(range(len(OB_LVLS))),
                "vVpE": list(range(len(VP_LOOKS))), "vOiE": list(range(len(OI_WINS5)))}


def family_keys(fam):
    """Candidate keys (besides the common ones) for a family, in P order."""
    F = FAMILIES[fam]
    return [F[s][0] for s in ("p1", "p2", "p3", "p4", "v1", "v2", "f1")]


def space_section(fam):
    """param_space.json-shaped section for a family (used for the shipped
    default space and by the panel's space editor)."""
    cont, menus, flags = {}, {}, {}
    order = 0
    for k, (kind, rng, doc) in COMMON.items():
        cont[k] = dict(range=list(rng), label=k, doc=doc, order=order); order += 1
    cont["leverage"] = dict(range=[1.2, 10.0], label="Leverage",
                            doc="Position leverage (lev mode).", order=order); order += 1
    for slot in ("p1", "p2", "p3", "p4", "v1", "v2", "f1"):
        k, kind, rng, doc = FAMILIES[fam][slot]
        if k in ("pad", "f1"):
            continue
        if kind in ("c", "i"):
            cont[k] = dict(range=list(rng), label=k, doc=doc, order=order,
                           **({"integer": True} if kind == "i" else {}))
        elif kind == "m":
            menus[k] = dict(options=[float(o) for o in rng],
                            labels=[str(o) for o in rng], doc=doc)
        elif kind == "f":
            flags[k] = dict(label=k, doc=doc)
        order += 1
    flags["eL"] = dict(label="Longs on/off", doc="Sampled on/off in lev mode.")
    flags["eS"] = dict(label="Shorts on/off", doc="Sampled on/off in lev mode.")
    return dict(continuous=cont, menus=menus, flags=flags)


MAX_TRADES = 60000


@njit(cache=True)
def _core_enr(fam, t_ms, o, h, l, c, obi, cvdz, vpd, poc, vah, val, oic, oiw,
              fok, regime, P, warmup, initial_capital, commission, no_entry,
              bph, no_long, no_short):
    """Shared bar-close state machine, next-bar-open fills.
    trade row: [entry_idx, exit_idx, dir, entry, exit, qty, net, mae, reason, lev]
    reason: 0=profit_target 1=stop_loss 2=LIQUIDATED 3=reversal 4=time_stop
            5=structure_exit."""
    n = len(c)
    eq = initial_capital
    pos = 0
    qty = 0.0; entry_px = 0.0
    entry_i = -1
    entry_tm = 0.0
    sl_price = np.nan
    mae = 0.0
    last_order_bar = -1e18
    pend_entry = 0
    pend_qty = 0.0; pend_sl = np.nan; pend_tm = 0.0
    pend_close = 0
    pend_lev = 1.0; cur_lev = 1.0
    liquidated = 0
    out = np.empty((MAX_TRADES, 10))
    nt = 0
    runL = 0; runS = 0          # poctrend consecutive-bar counters

    for i in range(n):
        # ---- fills at this bar's open ----
        if pend_close != 0 and pos != 0:
            px = o[i]
            net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
            eq += net
            if nt < MAX_TRADES:
                out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = pend_close - 1
                out[nt, 9] = cur_lev; nt += 1
            pos = 0; qty = 0.0; mae = 0.0
        if pend_entry != 0 and pos != 0 and pos != pend_entry:
            px = o[i]
            net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
            eq += net
            if nt < MAX_TRADES:
                out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = 3.0
                out[nt, 9] = cur_lev; nt += 1
            pos = 0; qty = 0.0; mae = 0.0
        if pend_entry != 0 and pos == 0:
            pos = pend_entry; qty = pend_qty; entry_px = o[i]; entry_i = i
            sl_price = pend_sl; entry_tm = pend_tm; mae = 0.0
            cur_lev = pend_lev
        pend_entry = 0; pend_close = 0

        # ---- intrabar tracking / liquidation ----
        if pos != 0:
            if pos > 0:
                adverse = l[i] / entry_px - 1.0
            else:
                adverse = 1.0 - h[i] / entry_px
            if adverse < mae:
                mae = adverse
            if cur_lev > 1.0:
                liq_move = 1.0 / cur_lev - 0.008
                if adverse <= -liq_move:
                    px = entry_px * (1 - liq_move * pos)
                    net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
                    eq += net
                    if nt < MAX_TRADES:
                        out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                        out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                        out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = 2.0
                        out[nt, 9] = cur_lev; nt += 1
                    pos = 0; qty = 0.0; mae = 0.0
                    liquidated = 1
                    break

        # ---- bar-close evaluation ----
        r = regime[i]
        tm = t_ms[i]
        blocked = (i < warmup) or (no_entry[i] == 1) or (fok[i] == 0) or i < 1
        p1 = P[r, 9]; p2 = P[r, 10]; p3 = P[r, 11]; p4 = P[r, 12]
        v1 = int(P[r, 13]); v2 = int(P[r, 14]); f1 = P[r, 15]
        longCond = False; shortCond = False
        exitL = False; exitS = False

        if fam == 0:                                   # ---- flowx ----
            z = cvdz[v1, i]; zp = cvdz[v1, i - 1]; b = obi[v2, i]
            if np.isfinite(z) and np.isfinite(zp) and np.isfinite(b):
                longCond = z >= p1 and zp < p1 and b >= p3
                shortCond = z <= -p1 and zp > -p1 and b <= -p3
                exitL = z <= -p2
                exitS = z >= p2
        elif fam == 1:                                 # ---- poctrend ----
            pc = poc[v1, i]; z = cvdz[v2, i]
            if np.isfinite(pc) and pc > 0:
                above = c[i] > pc * (1.0 + p1)
                below = c[i] < pc * (1.0 - p1)
                runL = runL + 1 if above else 0
                runS = runS + 1 if below else 0
                need = int(p3)
                if need < 1:
                    need = 1
                flowOk = (f1 <= 0) or (np.isfinite(z))
                longCond = runL == need and flowOk and (f1 <= 0 or z > 0.0)
                shortCond = runS == need and flowOk and (f1 <= 0 or z < 0.0)
                exitL = c[i] < pc * (1.0 - p2)
                exitS = c[i] > pc * (1.0 + p2)
            else:
                runL = 0; runS = 0
        elif fam == 2:                                 # ---- oisqueeze ----
            w = oiw[v1]
            oc = oic[v1, i]
            if i >= w and np.isfinite(oc) and c[i - w] > 0:
                ret = c[i] / c[i - w] - 1.0
                nb = int(p3)
                if nb < 1:
                    nb = 1
                if i >= nb:
                    hh = -1e300; ll = 1e300
                    for k in range(1, nb + 1):
                        if h[i - k] > hh:
                            hh = h[i - k]
                        if l[i - k] < ll:
                            ll = l[i - k]
                    longCond = oc >= p1 and ret <= -p2 and c[i] > hh
                    shortCond = oc >= p1 and ret >= p2 and c[i] < ll
            oc0 = oic[0, i]
            if np.isfinite(oc0) and oc0 <= -p4 and p4 > 0:
                exitL = True; exitS = True
        else:                                          # ---- absorb ----
            z = cvdz[v1, i]; b = obi[v2, i]; pc = poc[1, i]
            nb = int(p2)
            if nb < 1:
                nb = 1
            if np.isfinite(z) and np.isfinite(b) and np.isfinite(pc) and i >= nb:
                ll = 1e300; hh = -1e300
                for k in range(1, nb + 1):
                    if l[i - k] < ll:
                        ll = l[i - k]
                    if h[i - k] > hh:
                        hh = h[i - k]
                longCond = z <= -p1 and l[i] >= ll and b >= p3 and c[i] < pc * (1.0 - p4)
                shortCond = z >= p1 and h[i] <= hh and b <= -p3 and c[i] > pc * (1.0 + p4)
                exitL = c[i] >= pc
                exitS = c[i] <= pc

        longCond = longCond and P[r, 7] > 0 and no_long[i] == 0
        shortCond = shortCond and P[r, 8] > 0 and no_short[i] == 0
        can_open = (i - last_order_bar) > P[r, 4] * bph

        mark_eq = eq
        if pos != 0:
            mark_eq = eq + qty * (c[i] - entry_px) * pos

        if longCond and can_open and not blocked and pos != 1:
            pend_entry = 1
            pend_lev = P[r, 6]
            pend_qty = mark_eq * pend_lev / c[i]
            pend_sl = c[i] * (1 - P[r, 2]) if P[r, 2] > 0 else -1.0
            pend_tm = tm
            last_order_bar = i
        if pos == 1 and pend_close == 0:
            if sl_price > 0 and l[i] <= sl_price:
                pend_close = 2
            elif c[i] >= entry_px * (1 + P[r, 0]):
                pend_close = 1
            elif P[r, 5] > 0 and tm >= entry_tm + P[r, 5] * 3600000.0:
                pend_close = 5
            elif exitL:
                pend_close = 6
        if shortCond and can_open and not blocked and pos != -1:
            pend_entry = -1
            pend_lev = P[r, 6]
            pend_qty = mark_eq * pend_lev / c[i]
            pend_sl = c[i] * (1 + P[r, 3]) if P[r, 3] > 0 else -1.0
            pend_tm = tm
            last_order_bar = i
        if pos == -1 and pend_close == 0:
            if sl_price > 0 and h[i] >= sl_price:
                pend_close = 2
            elif c[i] <= entry_px * (1 - P[r, 1]):
                pend_close = 1
            elif P[r, 5] > 0 and tm >= entry_tm + P[r, 5] * 3600000.0:
                pend_close = 5
            elif exitS:
                pend_close = 6

    return (out[:nt], eq, liquidated, pos, entry_px, qty, entry_i, cur_lev,
            sl_price, entry_tm)


def _need(pre, key, shape2=None):
    if key not in pre:
        raise KeyError(f"enriched engine needs pre['{key}'] — run with "
                       f"LAB_FEATURES=ob,cvd,vp,oi so enriched.attach() adds it")
    return np.ascontiguousarray(np.asarray(pre[key], dtype=np.float64))


def run_enr_P(pre, P, fam, regime=None, warmup=0, initial_capital=1000.0,
              commission=0.0, no_entry=None, return_open=False,
              no_long=None, no_short=None):
    """Optimizer-path runner for one enriched family (per-regime P rows =
    ENR_PNAMES). Same return shape as run_macdx_P."""
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    ne = no_entry if no_entry is not None else np.zeros(n, dtype=np.int8)
    P = np.asarray(P, dtype=np.float64)
    if P.ndim == 1:
        P = P.reshape(1, -1)
    tf = float(os.environ.get("LAB_TF", "3"))
    oiw = np.array([max(1, int(round(w5 * 5 / tf))) for w5 in OI_WINS5], dtype=np.int64)
    fid = FAM_ID[fam] if isinstance(fam, str) else int(fam)
    arr, eq, liq, pos, epx, qty, ei, clev, slp, etm = _core_enr(
        fid, _need(pre, "t_ms"), _need(pre, "o"), _need(pre, "h"), _need(pre, "l"),
        _need(pre, "c"), _need(pre, "f_obi"), _need(pre, "f_cvdz"),
        _need(pre, "f_vpd"), _need(pre, "f_poc"), _need(pre, "f_vah"),
        _need(pre, "f_val"), _need(pre, "f_oic"), oiw,
        np.asarray(pre["f_ok"], dtype=np.int8),
        np.asarray(regime, dtype=np.int32), P,
        int(warmup), float(initial_capital), float(commission),
        np.asarray(ne, dtype=np.int8), 60.0 / tf,
        np.asarray(no_long if no_long is not None else np.zeros(n, dtype=np.int8), dtype=np.int8),
        np.asarray(no_short if no_short is not None else np.zeros(n, dtype=np.int8), dtype=np.int8))
    t = pre["t"]
    tr = pd.DataFrame(arr, columns=["entry_idx", "exit_idx", "dir", "entry",
                                    "exit", "qty", "net", "mae", "reason", "lev"])
    if len(tr):
        tr["entry_t"] = [str(t[int(k)])[:16] for k in tr["entry_idx"]]
        tr["exit_t"] = [str(t[int(k)])[:16] for k in tr["exit_idx"]]
    else:
        tr["entry_t"] = []; tr["exit_t"] = []
    open_pos = None
    if pos != 0 and not liq:
        open_pos = dict(dir=int(pos), entry=float(epx), qty=float(qty),
                        lev=float(clev), entry_t=str(t[int(ei)])[:16],
                        entry_idx=int(ei))
        try:
            r_now = int(np.asarray(regime, dtype=np.int64)[-1])
            tp = float(epx) * (1 + P[r_now, 0]) if pos > 0 else float(epx) * (1 - P[r_now, 1])
            open_pos["exit_proj"] = dict(tp=float(tp), sl=float(slp) if slp > 0 else None,
                                         kind="target")
        except Exception:
            pass
    if return_open:
        return tr, float(eq), bool(liq), open_pos
    return tr, float(eq), bool(liq)


# ---------------------------------------------------------------- genome ---
def sample_enr(fam):
    """Family sampler in the wf2 flat-candidate shape (per-regime lists)."""
    F = FAMILIES[fam]

    def _sample(rng, R, mode, space=None):
        s = space or {}

        def rr(k, d):
            try:
                return tuple(s["continuous"][k]["range"])
            except Exception:
                return d

        def g(k, d):
            lo, hi = rr(k, d)
            return [float(x) for x in rng.uniform(lo, hi, R)]

        c = dict(strategy=fam)
        for k, (kind, rng_, doc) in COMMON.items():
            c[k] = g(k, rng_)
        for slot in ("p1", "p2", "p3", "p4", "v1", "v2", "f1"):
            k, kind, rng_, doc = F[slot]
            if k == "pad":
                continue
            if k == "f1":
                continue
            if kind == "c":
                c[k] = g(k, rng_)
            elif kind == "i":
                lo, hi = rr(k, rng_)
                c[k] = [float(rng.integers(int(lo), int(hi) + 1)) for _ in range(R)]
            elif kind == "m":
                opts = ((s.get("menus", {}) or {}).get(k, {}) or {}).get("options") or rng_
                c[k] = [float(rng.choice(opts)) for _ in range(R)]
            elif kind == "f":
                c[k] = rng.choice([0.0, 1.0], R).tolist()
        c["eL"] = [1.0] * R
        if mode == "lev":
            c["lev"] = g("leverage", (1.2, 10.0))
            c["eS"] = [1.0] * R
            if rng.random() < 0.35:
                c["eS"] = rng.choice([0.0, 1.0], R, p=[0.25, 0.75]).tolist()
        else:
            c["lev"] = [1.0] * R
            c["eS"] = [0.0] * R
        return normalize_enr(c)
    return _sample


def normalize_enr(c):
    fam = c.get("strategy")
    if fam not in FAMILIES:
        return c
    R = len(c["tpL"])
    if isinstance(c.get("lev"), list):
        c["lev"] = [max(1.0, float(int(x))) for x in c["lev"]]
    for slot in ("p3", "p2"):
        k, kind, rng_, _ = FAMILIES[fam][slot]
        if kind == "i" and isinstance(c.get(k), list):
            lo, hi = rng_
            c[k] = [float(max(lo, min(hi, round(x)))) for x in c[k]]
    for k in ENR_MENU_KEYS:
        if isinstance(c.get(k), list):
            nopt = len(MENU_OPTIONS[k])
            c[k] = [float(max(0, min(nopt - 1, round(x)))) for x in c[k]]
    for k in ENR_FLAG_KEYS:
        if isinstance(c.get(k), list):
            c[k] = [1.0 if x >= 0.5 else 0.0 for x in c[k]]
    for k in ("slL", "slS", "maxH", "minT"):
        if isinstance(c.get(k), list):
            c[k] = [max(0.0, float(x)) for x in c[k]]
    return c


def build_P_enr(c, R):
    fam = c["strategy"]
    F = FAMILIES[fam]
    slot_key = {s: F[s][0] for s in ("p1", "p2", "p3", "p4", "v1", "v2", "f1")}
    rows = []
    for r in range(R):
        row = []
        for pn in ENR_PNAMES:
            key = slot_key.get(pn, pn)
            if key in ("pad", "f1") and key not in c:
                v = 0.0
            else:
                v = c.get(key, 0.0)
            row.append(float(v[r] if isinstance(v, list) else v))
        rows.append(row)
    return np.array(rows, dtype=np.float64)


def defaults_enr(fam, mode, R, space=None):
    """A deterministic 'defaults' candidate: range midpoints, first menu
    option, flags off — the quick-backtest / anchor starting point."""
    s = space or space_section(fam)
    cont = s.get("continuous") or {}
    c = dict(strategy=fam)
    for k in list(COMMON) + [FAMILIES[fam][slot][0] for slot in ("p1", "p2", "p3", "p4")]:
        if k == "pad":
            continue
        rng = (cont.get(k) or {}).get("range")
        if not rng:
            kind, rng_, _ = COMMON.get(k, (None, None, None)) if k in COMMON else \
                next(((kd, r, d) for kk, kd, r, d in FAMILIES[fam].values() if kk == k), (None, (0, 0), ""))
            rng = rng_
        mid = (float(rng[0]) + float(rng[1])) / 2.0
        c[k] = [mid] * R
    for slot in ("v1", "v2", "f1"):
        k, kind, opts, _ = FAMILIES[fam][slot]
        if k in ("pad", "f1"):
            continue
        if kind == "m":
            c[k] = [float(opts[min(1, len(opts) - 1)])] * R
        elif kind == "f":
            c[k] = [0.0] * R
    c["eL"] = [1.0] * R
    c["eS"] = [1.0 if mode == "lev" else 0.0] * R
    c["lev"] = [3.0 if mode == "lev" else 1.0] * R
    return normalize_enr(c)
