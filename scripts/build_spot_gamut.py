#!/usr/bin/env python3
"""Stage-1 SPOT source gamut for ALL pairs (btc/eth/doge/xrp/sui/hype/sol),
mirroring the perp pipeline's grid: 3 strategies x 3 methods x 3 scorings x
{m5,m7} x 4 holdout variants @100k = 216 specs per pair per tf.

Spot mode: --mode spot (no --lev-stops), spot candles, macdx picks up its
@spot ranges automatically from the pair's AI space (sol = default.ai.json).
Campaigns: spotg1m_0811 (EC2 boxes) and spotg3m_0811 (the two Macs).
Stage 2 comes from build_spot_merge_specs.py after these complete.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")

PAIRS = ["btc", "eth", "doge", "xrp", "sui", "hype", "sol"]
SC = {"cl": "classic", "un": "underwater", "wo": "worst_window"}
HOLDS = {
    "hN": [],
    "hA2508": ["--train-end", "2025-08-28"],
    "hL7": ["--holdout-days", "7"],
    "hO2411": ["--holdout-outside", "2024-11-16..2025-08-28"],
}

def space_for(pair, tf):
    v = os.path.join("param_spaces", "variants", f"{pair}_{tf}m.ai.json")
    if pair == "sol":
        v = os.path.join("param_spaces", "variants", "default.ai.json")
    assert os.path.exists(os.path.join(OPT, v)), f"missing space {v}"
    return v

for tf, camp in (("1", "spotg1m_0811"), ("3", "spotg3m_0811")):
    specs = []
    for pair in PAIRS:
        sp = space_for(pair, tf)
        for strat in ("v7", "scalpx2", "macdx"):
            for meth in ("vol3", "trend3", "volXtrend9"):
                for sc, scarg in SC.items():
                    for mh in ("5", "7"):
                        for hk, hargs in HOLDS.items():
                            name = (f"spg_{pair}{tf}m_{strat}_gen_s_{meth}"
                                    f"_{sc}_d50_m{mh}_{hk}_t100k")
                            cmd = ["python3", "optimize2_cli.py",
                                   "--strategy", strat, "--mode", "spot",
                                   "--method", meth, "--algo", "genetic",
                                   "--procs", "14", "--batch", "100",
                                   "--total", "100000", "--symbol", pair,
                                   "--tf", tf,
                                   "--gap-mode", "skip_contaminated",
                                   "--max-dd", "0.5",
                                   "--max-hold-days", mh,
                                   "--scoring", scarg, "--name", name,
                                   "--space", sp]
                            cmd += hargs
                            specs.append(dict(name=name, coin=pair, tf=tf,
                                              cmd=cmd, status="pending",
                                              ai_space=None))
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    plan = dict(config=dict(kind="spot-gamut", tf=tf, budget=100000,
                            pairs=PAIRS),
                specs=specs, made=__import__("time").strftime("%F %T"))
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(f"{len(specs)} specs -> optimizer/campaigns/{camp}/plan.json")
