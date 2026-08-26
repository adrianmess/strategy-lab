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
_FALLBACK = {"lev": 0.0004, "spot": 0.0005}
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


def per_side(mode, coin=None):
    """Taker rate per fill for mode ('lev'|'spot'), per coin when known."""
    coin = ((coin or os.environ.get("LAB_COIN") or "").upper()
            .replace("_USDT", "").replace("USDT", ""))
    d = _doc()
    if d:
        m = d.get("fut" if mode == "lev" else "spot") or {}
        r = m.get(coin) or {}
        v = r.get("taker")
        if v is not None and 0.0 <= float(v) < 0.01:
            return float(v)
    return _FALLBACK["lev" if mode == "lev" else "spot"]
