#!/usr/bin/env python3
"""Build a gamut-style plan of MERGE sweep legs — the cross-product of
(families x holdout types x scorings) — runnable by gamut_worker on any
machine (the two-Mac LAN rig, or a cloud box). Durable markers + skip-if-done
give free meet-in-the-middle: run it forward on one machine and --reverse on
the other.

Spec file (JSON):
{
  "campaign": "msweep_0811",
  "budget": 100000,
  "families": [
    {"base": "merge_ghype_v7_gh",           # leg names: <base>_<hX>_<sc>
     "sources": ["runA", "runB", ...],      # the runs to combine
     "merge_mode": "breed",                 # or "merge"
     "strategy": "v7", "mode": "lev", "method": "trend3",
     "symbol": "hype", "tf": "3",           # omit for SOL 3m
     "max_dd": 0.5, "max_hold_days": 7}
  ],
  "holdouts": ["hA", "hB", "hBtw", "hOut", "hAlt30"],   # optional (default all 5)
  "scorings": ["cls", "wor", "und"],                    # optional (default all 3)
  "dates": {"after": "2025-09-01", "before": "2025-09-01",
            "window": "2024-11-16..2025-08-28"}         # optional overrides
}

Usage:
  python3 scripts/build_merge_plan.py spec.json
  -> optimizer/campaigns/<campaign>/plan.json
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")

SC_ARG = {"cls": None, "wor": "worst_window", "und": "underwater"}


def hold_args(h, dates):
    after = dates.get("after", "2025-09-01")
    before = dates.get("before", "2025-09-01")
    window = dates.get("window", "2024-11-16..2025-08-28")
    if h == "hA":
        return ["--train-end", after]
    if h == "hB":
        return ["--holdout-before", before]
    if h == "hBtw":
        return ["--holdout-between", window]
    if h == "hOut":
        return ["--holdout-outside", window]
    if h.startswith("hAlt"):
        return ["--holdout-days", h[4:] or "30"]
    sys.exit(f"unknown holdout type '{h}'")


def main():
    spec = json.load(open(sys.argv[1]))
    camp = spec["campaign"]
    budget = int(spec.get("budget", 100000))
    holds = spec.get("holdouts", ["hA", "hB", "hBtw", "hOut", "hAlt30"])
    scores = spec.get("scorings", ["cls", "wor", "und"])
    dates = spec.get("dates", {})
    specs = []
    for fam in spec["families"]:
        for h in holds:
            for sc in scores:
                name = f"{fam['base']}_{h}_{sc}"
                cmd = ["python3", "optimize2_cli.py",
                       "--strategy", fam.get("strategy", "v7"),
                       "--algo", "genetic", "--mode", fam.get("mode", "lev"),
                       "--method", fam.get("method", "vol3"),
                       "--procs", "14", "--batch", "100",
                       "--name", name, "--total", str(budget),
                       "--max-dd", str(fam.get("max_dd", 0.5)),
                       "--max-hold-days", str(fam.get("max_hold_days", 7)),
                       "--gap-mode", "skip_contaminated", "--sticky-oos",
                       "--resume-from",
                       ",".join(f"runs/{s}" for s in fam["sources"]),
                       "--merge-mode", fam.get("merge_mode", "breed")]
                if fam.get("symbol"):
                    cmd += ["--symbol", fam["symbol"]]
                if fam.get("tf"):
                    cmd += ["--tf", str(fam["tf"])]
                if SC_ARG.get(sc):
                    cmd += ["--scoring", SC_ARG[sc]]
                specs.append(dict(name=name, cmd=cmd))
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    json.dump(specs, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(f"{len(specs)} merge legs -> optimizer/campaigns/{camp}/plan.json")
    print("run:  python3 gamut_worker.py --plan "
          f"campaigns/{camp}/plan.json --jobs N [--reverse]")


if __name__ == "__main__":
    main()
