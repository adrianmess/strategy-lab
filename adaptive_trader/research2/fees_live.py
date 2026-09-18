"""Per-side commission rates, sourced from the exchange.

adaptive_trader/fees.json is refreshed hourly by the panel with MEXC's own
numbers (futures: public contract detail; spot: signed /api/v3/tradeFee), so
fee changes on the exchange propagate everywhere automatically — engines,
optimizer CLIs and the live fcfs hosts all read through here. When the file
or the pair is missing, falls back to the historic conservative constants the
research stack has always used (0.04%/side lev, 0.05%/side spot).

Fees are PER-SYMBOL on MEXC (several pairs are currently 0-fee promos), so
callers pass the coin when they know it; otherwise LAB_COIN env is used.
Taker is the right per-side model: live entries and exits are market orders.
"""
import json
import os

_P = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  os.pardir, "fees.json")
# Used only when fees.json is missing or has no entry for the coin — which
# is the normal state on an off-box worker. These are MEXC's published API
# schedules, NOT the web rates: futures API is its own dearer schedule
# (0.06/0.08, effective 2026-06-01, overriding web rates and promos), while
# spot API keeps the standard spot rates (0% maker / 0.05% taker). The old
# single 0.0004 for lev was half the real futures taker, so a machine
# without fees.json quietly modelled every futures strategy at half cost.
_FALLBACK = {("lev", "taker"): 0.0008, ("lev", "maker"): 0.0006,
             ("spot", "taker"): 0.0005, ("spot", "maker"): 0.0}
_cache = {"mt": None, "doc": None}


def _doc():
    try:
        mt = os.path.getmtime(_P)
        if _cache["mt"] != mt:
            _cache["doc"] = json.load(open(_P))
            _cache["mt"] = mt
    except Exception:
        _cache["doc"] = None
    return _cache["doc"]


def per_side(mode, coin=None, side=None):
    """Commission per fill for mode ('lev'|'spot'), per coin when known.

    side='taker' (market orders — the default, and what a market entry/exit
    actually pays) or 'maker' (resting limit orders; MEXC futures maker is
    currently 0). Omit it and LAB_FEE_SIDE decides, so a whole simulation can
    be costed as maker without threading the argument everywhere.
    LAB_FEE_OVERRIDE (fraction/side, e.g. '0.0008') wins over both — that is
    the Backtests page's what-if fee box and the combo builder's manual rate.
    """
    ov = os.environ.get("LAB_FEE_OVERRIDE")
    if ov:
        try:
            v = float(ov)
            if 0.0 <= v < 0.01:
                return v
        except ValueError:
            pass
    side = (side or os.environ.get("LAB_FEE_SIDE") or "taker").lower()
    if side not in ("taker", "maker"):
        side = "taker"
    coin = ((coin or os.environ.get("LAB_COIN") or "").upper()
            .replace("_USDT", "").replace("USDT", ""))
    d = _doc()
    if d:
        m = d.get("fut" if mode == "lev" else "spot") or {}
        r = m.get(coin) or {}
        v = r.get(side)
        if v is not None and 0.0 <= float(v) < 0.01:
            return float(v)
    return _FALLBACK[("lev" if mode == "lev" else "spot", side)]
