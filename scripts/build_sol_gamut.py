#!/usr/bin/env python3
"""Stage-1 SOL source gamut: fresh searches mirroring the g0801_2122 grid
shape (SOL was never in the gamut — it's the reference pair, so the space is
param_spaces/variants/default.ai.json for both tfs).

Grid per tf: 3 strategies x 3 methods x 3 scorings x {m5,m7} x 4 holdouts
@100k = 216 specs. Campaigns: solg1m_0811 (EC2), solg3m_0811 (Macs).
Stage 2 (merge sweep of each family's top-8) is built by
build_sol_merge_specs.py after this completes.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")

SC = {"cl": "classic", "un": "underwater", "wo": "worst_window"}
HOLDS = {
    "hN": [],
    "hA2508": ["--train-end", "2025-08-28"],
    "hL7": ["--holdout-days", "7"],
    "hO2411": ["--holdout-outside", "2024-11-16..2025-08-28"],
}

for tf, camp in (("1", "solg1m_0811"), ("3", "solg3m_0811")):
    specs = []
    for strat in ("v7", "scalpx2", "macdx"):
        for meth in ("vol3", "trend3", "volXtrend9"):
            for sc, scarg in SC.items():
                for mh in ("5", "7"):
                    for hk, hargs in HOLDS.items():
                        name = (f"solg_sol{tf}m_{strat}_gen_l_{meth}_{sc}"
                                f"_d50_m{mh}_{hk}_t100k")
                        cmd = ["python3", "optimize2_cli.py",
                               "--strategy", strat, "--mode", "lev",
                               "--method", meth, "--algo", "genetic",
                               "--procs", "14", "--batch", "100",
                               "--total", "100000", "--symbol", "sol",
                               "--tf", tf, "--gap-mode", "skip_contaminated",
                               "--max-dd", "0.5", "--max-hold-days", mh,
                               "--scoring", scarg, "--name", name,
                               "--lev-stops", "--space",
                               "param_spaces/variants/default.ai.json"]
                        cmd += hargs
                        specs.append(dict(name=name, coin="sol", tf=tf,
                                          cmd=cmd, status="pending",
                                          ai_space=None))
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    plan = dict(config=dict(kind="sol-gamut", tf=tf, budget=100000),
                specs=specs, made=__import__("time").strftime("%F %T"))
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(f"{len(specs)} specs -> optimizer/campaigns/{camp}/plan.json")
