#!/usr/bin/env python3
"""Stage-2 SOL merge sweep builder. Run AFTER the solg gamuts complete.
Ranks each (strategy x method) family's stage-1 runs by honest growth
(holdout_best_config growth, fallback best_config metrics growth — mirrors
the runs2-cache ranking msweep/m1sweep used), takes top-8, and emits specs
sol1w_0811 (1m, EC2) / sol3w_0811 (3m, Macs) for build_merge_plan.py.
"""
import json, glob, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")

for tf, camp, base_p in (("1", "sol1w_0811", "s1w"), ("3", "sol3w_0811", "s3w")):
    groups = collections.defaultdict(list)
    for p in glob.glob(os.path.join(OPT, "runs", f"solg_sol{tf}m_*")):
        name = os.path.basename(p)
        try:
            parts = name.split("_")          # solg sol1m v7 gen l vol3 ...
            strat, meth = parts[2], parts[5]
            g = None
            hp = os.path.join(p, "holdout_best_config.json")
            if os.path.exists(hp):
                h = json.load(open(hp))
                g = ((h.get("holdout") or {}).get("growth")
                     or (h.get("metrics") or {}).get("growth"))
            if g is None:
                b = json.load(open(os.path.join(p, "best_config.json")))
                g = (b.get("metrics") or {}).get("growth")
            if g is not None:
                groups[(strat, meth)].append((g, name))
        except Exception:
            continue
    fams = []
    for (strat, meth), v in sorted(groups.items()):
        src = [n for _, n in sorted(v, reverse=True)[:8]]
        if len(src) < 2:
            print(f"SKIP {strat}/{meth}: only {len(src)} usable sources")
            continue
        fams.append(dict(base=f"{base_p}_sol{tf}m_{strat}_{meth}",
                         sources=src, merge_mode="breed", strategy=strat,
                         mode="lev", method=meth, symbol="sol", tf=tf,
                         max_dd=0.5, max_hold_days=7))
    spec = dict(campaign=camp, budget=100000, families=fams)
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    json.dump(spec, open(os.path.join(out, "spec.json"), "w"), indent=1)
    srcs = sorted({s for f in fams for s in f["sources"]})
    open(os.path.join(out, "sources.txt"), "w").write("\n".join(srcs) + "\n")
    print(f"{camp}: {len(fams)} families, {len(fams)*15} legs, "
          f"{len(srcs)} sources -> spec.json (now run build_merge_plan.py)")
