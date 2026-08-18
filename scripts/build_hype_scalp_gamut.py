#!/usr/bin/env python3
"""HYPE SPOT scalp gamut — the honest version of "trade the fluctuations":
instead of hand-rolled mean reversion, put Adrian's OWN scalp families
through the full search machinery on HYPE spot 1m.

Deliberately covers ground the earlier spotg1m campaign did NOT: it ran
genetic-only over {v7, scalpx2, macdx} x {vol3,trend3,volXtrend9} x 3
scorings x 4 holdouts. This one adds the crossfit algorithm, the cvol7
regime, the original scalpx family, and a different holdout calendar.

  strategy {scalpx2, scalpx}
x algo     {genetic, crossfit}
x regime   {vol3, trend3, volXtrend9, cvol7}
x scoring  {classic, underwater, worst_window}
x holdout  {after, before, between, outside, alternating-30d}   = 240 specs

Two shards so the mini (8 procs) and MacBook (12 procs) run concurrently;
each is a standard gamut plan, so gamut_worker/Progress/pause all work.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "..", "optimizer")

SC = {"cl": "classic", "un": "underwater", "wo": "worst_window"}
# the five canonical holdout shapes (same family the gauntlet uses)
HOLDS = {
    "hA": ["--train-end", "2026-02-01"],
    "hB": ["--holdout-before", "2025-06-01"],
    "hBtw": ["--holdout-between", "2025-09-01..2025-12-01"],
    "hOut": ["--holdout-outside", "2025-06-01..2026-02-01"],
    "hAlt30": ["--holdout-days", "30"],
}
STRATS = ["scalpx2", "scalpx"]
ALGOS = {"gen": "genetic", "cro": "crossfit"}
METHODS = ["vol3", "trend3", "volXtrend9", "cvol7"]
TF = "1"
SPACE = os.path.join("param_spaces", "variants", f"hype_{TF}m.ai.json")

specs = []
for strat in STRATS:
    for ak, algo in ALGOS.items():
        for meth in METHODS:
            for sk, scarg in SC.items():
                for hk, hargs in HOLDS.items():
                    name = f"hys_{TF}m_{strat}_{ak}_{meth}_{sk}_{hk}_t60k"
                    cmd = ["python3", "optimize2_cli.py",
                           "--strategy", strat, "--mode", "spot",
                           "--method", meth, "--algo", algo,
                           "--procs", "4", "--batch", "100",
                           "--total", "60000", "--symbol", "hype", "--tf", TF,
                           "--gap-mode", "skip_contaminated",
                           "--max-dd", "0.5", "--max-hold-days", "7",
                           "--scoring", scarg, "--name", name,
                           "--space", SPACE] + hargs
                    specs.append(dict(name=name, coin="hype", tf=TF, cmd=cmd,
                                      status="pending", ai_space=None))

assert os.path.exists(os.path.join(OPT, SPACE)), SPACE
mini = [s for i, s in enumerate(specs) if i % 5 < 2]      # 8 procs  -> 2/5
mac = [s for i, s in enumerate(specs) if i % 5 >= 2]      # 12 procs -> 3/5
for camp, shard in (("hyscalp_mini", mini), ("hyscalp_mac", mac)):
    out = os.path.join(OPT, "campaigns", camp)
    os.makedirs(out, exist_ok=True)
    json.dump(dict(config=dict(kind="hype-spot-scalp-gamut", tf=TF,
                               budget=60000, pairs=["hype"]),
                   specs=shard, made=time.strftime("%F %T")),
              open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(f"{len(shard):>4} specs -> optimizer/campaigns/{camp}/plan.json")
print(f"{len(specs)} total ({len(STRATS)} strat x {len(ALGOS)} algo x "
      f"{len(METHODS)} regime x {len(SC)} scoring x {len(HOLDS)} holdout, "
      f"HYPE spot {TF}m @60k evals)")
