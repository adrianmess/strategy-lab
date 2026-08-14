#!/usr/bin/env python3
"""Stage-2 SPOT merge sweeps. Run AFTER spotg1m/spotg3m complete.
For every (pair x strategy x method) family: rank its stage-1 spot runs by
honest growth (holdout_best growth, fallback best_config metrics growth —
the same ranking the lev sweeps used), take the top 8 as merge sources, and
emit specs sp1w_0814 (1m, EC2) / sp3w_0814 (3m, Macs) for build_merge_plan.
Mode is SPOT — the merges inherit it from the family dicts.
"""
import collections
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")
PAIRS = ["btc", "eth", "doge", "xrp", "sui", "hype", "sol"]

for tf, camp, base_p in (("1", "sp1w_0814", "sp1w"), ("3", "sp3w_0814", "sp3w")):
    fams = []
    skipped = []
    for pair in PAIRS:
        groups = collections.defaultdict(list)
        for p in glob.glob(os.path.join(OPT, "runs", f"spg_{pair}{tf}m_*")):
            name = os.path.basename(p)
            try:
                parts = name.split("_")   # spg btc1m v7 gen s vol3 ...
                strat, meth = parts[2], parts[5]
                g = None
                hp = os.path.join(p, "holdout_best_config.json")
                if os.path.exists(hp):
                    h = json.load(open(hp))
                    g = ((h.get("holdout") or {}).get("growth")
                         or (h.get("metrics") or {}).get("growth"))
                if g is None:
                    bp = os.path.join(p, "best_config.json")
                    if not os.path.exists(bp):
                        continue          # no_survivor legs have no genome
                    b = json.load(open(bp))
                    g = (b.get("metrics") or {}).get("growth")
                if g is not None:
                    groups[(strat, meth)].append((g, name))
            except Exception:
                continue
        for (strat, meth), v in sorted(groups.items()):
            src = [n for _, n in sorted(v, reverse=True)[:8]]
            if len(src) < 2:
                skipped.append(f"{pair}/{strat}/{meth} ({len(src)})")
                continue
            fams.append(dict(base=f"{base_p}_{pair}{tf}m_{strat}_{meth}",
                             sources=src, merge_mode="breed", strategy=strat,
                             mode="spot", method=meth, symbol=pair, tf=tf,
                             max_dd=0.5, max_hold_days=7))
    spec = dict(campaign=camp, budget=100000, families=fams)
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    json.dump(spec, open(os.path.join(out, "spec.json"), "w"), indent=1)
    srcs = sorted({s for f in fams for s in f["sources"]})
    open(os.path.join(out, "sources.txt"), "w").write("\n".join(srcs) + "\n")
    print(f"{camp}: {len(fams)} families, {len(fams) * 15} legs, "
          f"{len(srcs)} sources"
          + (f" | skipped thin families: {skipped}" if skipped else ""))
