"""Risk classification for published backtest entries: leverage + stop-loss.

risk_of(entry) -> (lev_x, sl_class)

- lev_x: the MAX leverage the config trades (per-regime arrays flattened),
  1.0 for spot, None when undeterminable (router combos etc.).
- sl_class: 'stops'    — stop-loss active in every regime, or it actually
                          fired in the sim (stats.sl_hits > 0)
            'partial'  — active in SOME regimes only (per-regime arrays with
                          zeroed slots, masked trailing stops, ...)
            'stopless' — disabled/zero everywhere and never fired
            None       — no config to judge (router/combo entries)

Family knowledge (from the research engines, 2026-09-06):
- scalpx/scalpx2: `sl` magnitude gated by the `slOn` enable flag.
- rocx: trailing stop `tsl` gated by the per-regime `tslm` mask.
- macdx: per-regime `slL`/`slS` arrays (0 = off in that regime); the pine
  originals use stopLossLongPct/stopLossShortPct.
- v5/v6/scalp brackets: slLong/slShort/xSlLong/xSlShort magnitudes.
- v7/prime7: no stop-loss parameters at all (profit-target exits only) —
  classified stopless unless the sim recorded sl hits.

The classification is a HEURISTIC over config params (>0 == active); the
empirical sl_hits count always wins upward (hits with a config that reads
stopless -> partial). Used by the panel at fold time and by
scripts/backfill_risk.py for the historical store.
"""

_ROUTERS = {"fcfsx", "metax", "metax2", "pairx"}
_MAG_KEYS = ("slL", "slS", "sl", "slLong", "slShort", "xSlLong", "xSlShort",
             "stopLossLongPct", "stopLossShortPct")


def _vals(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                pass
        return out
    try:
        return [float(v)]
    except (TypeError, ValueError):
        return []


def _pick(vs, i):
    return vs[i] if i < len(vs) else (vs[-1] if vs else 0.0)


def risk_of(entry):
    cfg = entry.get("config") or {}
    cand = cfg.get("cand") if isinstance(cfg.get("cand"), dict) else cfg
    if not isinstance(cand, dict):
        cand = {}
    strat = (entry.get("strategy") or cand.get("strategy") or "").lower()
    mode = entry.get("mode")
    hits = int((entry.get("stats") or {}).get("sl_hits") or 0)

    # ---- leverage ----
    if mode == "spot":
        lev = 1.0
    else:
        lev = max(_vals(cand.get("leverage")) + _vals(cand.get("lev"))
                  or [0.0])
        if not lev:
            tl = [t.get("lev") for t in (entry.get("trades") or [])
                  if isinstance(t, dict) and t.get("lev")]
            lev = max(_vals(tl) or [0.0])
        lev = lev or None

    if strat in _ROUTERS:
        return lev, None
    if not cand:
        # no config to judge — only the empirical signal is trustworthy
        return lev, ("partial" if hits > 0 else None)

    # ---- stop-loss: per-regime activity flags ----
    flags = []
    if "slOn" in cand:                       # scalpx family: sl + enable
        on, mag = _vals(cand.get("slOn")), _vals(cand.get("sl"))
        for i in range(max(len(on), len(mag), 1)):
            flags.append(_pick(on, i) > 0 and _pick(mag, i) > 0)
    elif "tslm" in cand or "tsl" in cand:    # rocx: trailing stop + mask
        m = _vals(cand.get("tslm")) or [1.0]
        t = _vals(cand.get("tsl"))
        for i in range(max(len(m), len(t), 1)):
            flags.append(_pick(m, i) > 0 and _pick(t, i) > 0)
    else:
        for k in _MAG_KEYS:
            if k in cand:
                flags.extend(v > 0 for v in _vals(cand[k]))

    if flags:
        slc = ("stops" if all(flags)
               else "partial" if any(flags) else "stopless")
    else:
        slc = "stopless"                     # no SL params (v7/prime7 style)
    if hits > 0 and slc == "stopless":
        slc = "partial"                      # the sim disagrees — trust it
    return lev, slc
