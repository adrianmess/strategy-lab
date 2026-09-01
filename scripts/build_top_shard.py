#!/usr/bin/env python3
"""Build a refresh shard: the top-N published backtests per pair x mode
(ranked by monthly growth), 1m + 3m timeframes, single-strategy runs only,
with each entry's genome embedded so an OFF-BOX worker (MacBook offload)
needs no access to optimizer/runs.

Run ON THE MINI (it owns backtests.js + the runs tree):
  ~/venv/bin/python3 scripts/build_top_shard.py [N=20] [out.json]
"""
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), ".."))
OPT = os.path.join(REPO, "optimizer")
SKIP = {"metax", "metax2", "pairx", "fcfsx"}   # combos have no single genome
_SUF = ["_oosbest_full", "_best_full", "_full"]
_GEN = {"_oosbest_full": "holdout_best_config.json",
        "_best_full": "best_config.json", "_full": "best_config.json"}


def genome_of(e):
    g = e.get("config")
    if g:
        return g
    name = e.get("name") or ""
    for s in _SUF:
        if name.endswith(s):
            r = name[:-len(s)]
            gp = os.path.join(OPT, "runs", r, _GEN[s])
            if not os.path.exists(gp):
                gp = os.path.join(OPT, "runs", r, "best_config.json")
            if os.path.exists(gp):
                return json.load(open(gp))
            break
    return None


def main():
    n_top = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    out_p = (sys.argv[2] if len(sys.argv) > 2 else
             os.path.join(REPO, "dashboard", "bt_refresh", "top_pm.json"))
    txt = open(os.path.join(REPO, "dashboard", "backtests.js")).read()
    entries = json.JSONDecoder().raw_decode(
        txt[txt.index("=") + 1:].lstrip())[0]
    groups = {}
    for e in entries:
        st = e.get("stats") or {}
        tf = str(e.get("timeframe") or "").rstrip("m")
        if (e.get("strategy") in SKIP or tf not in ("1", "3")
                or st.get("monthly_growth_pct") is None):
            continue
        pair = (e.get("pair") or "").split("_")[0]
        mode = e.get("mode")
        if not pair or mode not in ("lev", "spot"):
            continue
        groups.setdefault((pair, mode), []).append(e)
    items, skipped = [], 0
    for key in sorted(groups):
        es = sorted(groups[key],
                    key=lambda e: -(e["stats"].get("monthly_growth_pct")
                                    or -1e9))
        n = 0
        for e in es:
            if n >= n_top:
                break
            g = genome_of(e)
            if not g:
                skipped += 1
                continue
            items.append(dict(name=e["name"], pair=e.get("pair"),
                              timeframe=str(e.get("timeframe") or ""),
                              mode=e.get("mode"), method=e.get("method"),
                              strategy=e.get("strategy"), kind=e.get("kind"),
                              opt=e.get("opt"), genome=g))
            n += 1
    os.makedirs(os.path.dirname(out_p), exist_ok=True)
    json.dump(items, open(out_p, "w"), default=float)
    print(f"shard: {out_p}")
    print(f"items: {len(items)} across {len(groups)} pair x mode groups "
          f"(top {n_top} each); {skipped} skipped (no genome)")


if __name__ == "__main__":
    main()
