#!/usr/bin/env python3
"""Generate per-pair parameter-range overlays (param_spaces/<coin>.json).

Method (2026-07, per Adrian's spec):
- %-denominated thresholds (profit targets, stops, adverse-move triggers,
  cooldown price drops) scale with the pair's MEDIAN DAILY REALIZED VOL
  (spot 3-min candles) relative to SOL: a 0.83% dip on ETH "is" a 1.1% dip
  on SOL. z-scored, unitless (%B, RSI), duration and length params are
  self-normalizing and stay at SOL ranges.
- macdx mMinS/mMaxL are RAW price-unit MACD thresholds -> scale by the
  pair's MACD(12/26) standard deviation ratio (price level x vol).
- BTC/ETH: sanity-anchored against Adrian's reference TradingView Prime
  configs (PT 0.83%, SL 1%, cooldown 0.5%/60min, apt 0.5/0.3%) — the scaled
  ranges must CONTAIN the vol-scaled reference values; ranges are widened if
  ever needed (asserted below).

Rerun after data refreshes to update the saved defaults.
"""
import json, os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "adaptive_trader", "research", "data")
OUT = os.path.join(HERE, "param_spaces")
COINS = ["btc", "eth", "doge", "xrp", "sui", "hype"]

# %-denominated continuous params per family (everything else stays SOL-range)
PCT_KEYS = {
    "v7":     ["apt1Long", "apt1Short", "apt2Long", "apt2Short",
               "cdPctLong", "cdPctShort", "ptLong", "ptShort",
               "slLong", "slShort", "xApt1Long", "xApt1Short",
               "xApt2Long", "xApt2Short", "xTpLong", "xTpShort",
               "xCdPctShort"],
    "prime7": ["apt1Long", "apt1Short", "apt2Long", "apt2Short",
               "cdPctLong", "cdPctShort", "ptLong", "ptShort",
               "slLong", "slShort"],
    "prime":  ["ptLong", "ptShort", "apt1Long", "apt1Short", "apt2Long",
               "apt2Short", "cdPctLong", "cdPctShort", "slLong", "slShort"],
    "macdx":  ["tpL", "tpS", "slL", "slS", "a1L", "a2L", "a1S", "a2S",
               "cdPL", "cdPS"],
    "rocx":   ["pt", "tsl"],
    "scalpx2": ["tpL", "tpS"],
    "scalpx": ["tpL", "tpS"],
}
RAW_MACD_KEYS = {"macdx": ["mMinS", "mMaxL"]}

# ETH reference config (TradingView export 2026-07-27), %-values as fractions
ETH_REF = dict(ptLong=0.0083, slLong=0.01, apt1Long=0.005, apt2Long=0.003,
               cdPctLong=0.005)
BTC_REF = dict(ptLong=0.0083, slLong=0.01)


def pair_stats():
    out = {}
    for c in ["sol"] + COINS:
        p = os.path.join(DATA, f"{c}_spot_3min.parquet")
        if not os.path.exists(p):     # perp-only pairs (e.g. hype)
            p = os.path.join(DATA, f"{c}_3min.parquet")
        px = pd.read_parquet(p)["close"].astype(float)
        r = np.log(px).diff()
        dvol = (r.rolling(480).std() * np.sqrt(480)).dropna().median()
        ema = lambda s, n: s.ewm(span=n, adjust=False).mean()
        macd_std = float((ema(px, 12) - ema(px, 26)).std())
        out[c] = dict(dvol=float(dvol), macd_std=macd_std)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    stats = pair_stats()
    base = stats["sol"]
    spaces = {k: v for k, v in json.load(
        open(os.path.join(HERE, "param_space.json"))).items()}
    for c in COINS:
        vr = stats[c]["dvol"] / base["dvol"]
        mr = stats[c]["macd_std"] / base["macd_std"]
        ov = {"_meta": dict(vol_ratio_vs_sol=round(vr, 3),
                            macd_scale_vs_sol=round(mr, 4),
                            generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
                            method="median daily realized vol (spot 3min) for %-params; "
                                   "MACD(12/26) std for raw-unit params")}
        for fam, keys in PCT_KEYS.items():
            for variant in (fam, f"{fam}@spot"):
                sp = spaces.get(variant)
                if not sp:
                    continue
                cont = sp.get("continuous") or {}
                famov = ov.setdefault(variant, {})
                for k in keys:
                    if k in cont and "range" in cont[k]:
                        lo, hi = cont[k]["range"]
                        famov[k] = [round(lo * vr, 6), round(hi * vr, 6)]
        for fam, keys in RAW_MACD_KEYS.items():
            for variant in (fam, f"{fam}@spot"):
                sp = spaces.get(variant)
                if not sp:
                    continue
                cont = sp.get("continuous") or {}
                famov = ov.setdefault(variant, {})
                for k in keys:
                    if k in cont and "range" in cont[k]:
                        lo, hi = cont[k]["range"]
                        famov[k] = [round(lo * mr, 6), round(hi * mr, 6)]
        # reference sanity: the scaled prime ranges must contain the
        # vol-scaled reference values from Adrian's TradingView configs
        ref = ETH_REF if c == "eth" else BTC_REF if c == "btc" else {}
        for k, v in ref.items():
            for variant in ("prime", "prime7", "v7"):
                famov = ov.get(variant) or ov.get(f"{variant}@spot")
                if famov and k in famov:
                    lo, hi = famov[k]
                    if not (lo <= v <= hi):
                        famov[k] = [round(min(lo, v * 0.5), 6),
                                    round(max(hi, v * 1.5), 6)]
                        print(f"  {c}/{variant}/{k}: widened to contain "
                              f"reference {v}")
        p = os.path.join(OUT, f"{c}.json")
        json.dump(ov, open(p, "w"), indent=1)
        n = sum(len(v) for k, v in ov.items() if k != "_meta")
        print(f"{c}: vol x{vr:.2f}, macd x{mr:.4f} -> {n} ranges -> {p}")


def _dd(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _ext_len(grid, kind):
    """Extended grid length on 1m charts (exact replica of the engines'
    3x-dedupe: same wall-clock spans become searchable)."""
    if kind == "bb":
        ext = grid + [(g[0] * 3, g[1]) for g in grid]
    elif kind in ("macd", "xmacd"):
        ext = grid + [tuple(y * 3 for y in g) for g in grid]
    else:
        ext = grid + [g * 3 for g in grid]
    return len(_dd([tuple(g) if isinstance(g, (list, tuple)) else g
                    for g in ext]))


def materialize():
    """Full per-pair-per-timeframe space files: param_spaces/<coin>_<tf>m.json
    (same schema as param_space.json, independently editable). SOL 3m stays
    the canonical param_space.json. Pair scaling baked in; 1m files also
    materialize the widened variant menus so they're visible/editable."""
    stats = pair_stats()
    base_all = json.load(open(os.path.join(HERE, "param_space.json")))
    import copy
    v7v = (base_all.get("v7", {}).get("variants")
           or dict(rsi=[2, 3, 4, 6, 9, 14],
                   macd=[[3, 7], [3, 10], [5, 13], [8, 17], [12, 26]],
                   bb=[[14, 2], [21, 2], [30, 2], [21, 2.5], [50, 2]],
                   ema=[20, 50, 95, 150, 270],
                   xmacd=[[12, 26, 9], [12, 26, 8], [8, 17, 9], [5, 35, 5], [20, 50, 9]],
                   histn=[2, 4, 8, 12]))
    s2v = (base_all.get("scalpx2", {}).get("variants")
           or dict(rsi=[7, 10, 14, 21], cvd=[25, 50, 100],
                   poc=[150, 300, 600], emaS=[400, 800, 1200, 2000]))
    menu_grids = {
        ("v7", "vRsi"): ("rsi", v7v["rsi"]), ("v7", "vMacd"): ("macd", v7v["macd"]),
        ("v7", "vBB"): ("bb", v7v["bb"]), ("v7", "vEmaUp"): ("ema", v7v["ema"]),
        ("v7", "vEmaDn"): ("ema", v7v["ema"]), ("v7", "vX"): ("xmacd", v7v["xmacd"]),
        ("v7", "vHistN"): ("histn", v7v["histn"]),
        ("scalpx2", "vR"): ("i", s2v["rsi"]), ("scalpx2", "vC"): ("i", s2v["cvd"]),
        ("scalpx2", "vP"): ("i", s2v["poc"]), ("scalpx2", "vE"): ("i", s2v["emaS"]),
        ("rocx", "vRoc"): ("i", [7, 10, 14, 21, 28]),
        ("rocx", "vSma"): ("i", [9, 13, 21, 34]),
    }
    for c in ["sol"] + COINS:
        vr = stats[c]["dvol"] / stats["sol"]["dvol"]
        mr = stats[c]["macd_std"] / stats["sol"]["macd_std"]
        for tf in (1, 3, 5):
            if c == "sol" and tf == 3:
                continue        # canonical param_space.json
            sp = copy.deepcopy(base_all)
            if c != "sol":
                for fam, keys in PCT_KEYS.items():
                    for var in (fam, f"{fam}@spot"):
                        cont = (sp.get(var) or {}).get("continuous") or {}
                        for k in keys:
                            if k in cont and "range" in cont[k]:
                                lo, hi = cont[k]["range"]
                                cont[k]["range"] = [round(lo * vr, 6),
                                                    round(hi * vr, 6)]
                for fam, keys in RAW_MACD_KEYS.items():
                    for var in (fam, f"{fam}@spot"):
                        cont = (sp.get(var) or {}).get("continuous") or {}
                        for k in keys:
                            if k in cont and "range" in cont[k]:
                                lo, hi = cont[k]["range"]
                                cont[k]["range"] = [round(lo * mr, 6),
                                                    round(hi * mr, 6)]
            if tf == 1:
                for (famroot, mk), (kind, grid) in menu_grids.items():
                    n = _ext_len([tuple(g) if isinstance(g, list) else g
                                  for g in grid], kind)
                    for var in (famroot, f"{famroot}@spot", "prime7",
                                "prime7@spot"):
                        if famroot != "v7" and var.startswith("prime7"):
                            continue
                        m = ((sp.get(var) or {}).get("menus") or {}).get(mk)
                        if m and len(m.get("options") or []) < n:
                            m["options"] = list(range(n))
                            m["labels"] = [str(i) for i in range(n)]
            sp["_meta"] = dict(pair=f"{c.upper()}_USDT", timeframe=f"{tf}m",
                               vol_ratio_vs_sol=round(vr, 3),
                               macd_scale_vs_sol=round(mr, 4),
                               generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
                               note="independently editable — searches for this "
                                    "pair+timeframe use THIS file (unless a "
                                    "campaign passes an explicit --space)")
            p = os.path.join(OUT, f"{c}_{tf}m.json")
            json.dump(sp, open(p, "w"), indent=1)
            print(f"materialized {os.path.basename(p)}")


if __name__ == "__main__":
    main()
    materialize()
