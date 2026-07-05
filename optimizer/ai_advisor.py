#!/usr/bin/env python3
"""AI advisor — uses the Anthropic API to propose new candidate parameter
sets, seeded by what the optimizer has already learned. The suggestions are
evaluated like any other candidate and merged into the run's pool (they only
survive if they actually score well — the AI gets no special treatment).

Setup: put your key in the environment or in a .env file at the repo root:
    ANTHROPIC_API_KEY=sk-ant-...

Usage:
    python3 ai_advisor.py --run runs/study_lev_vol3_gen --n 12
"""
import _bootstrap as B
import argparse, json, os, re
import numpy as np
import requests

MODELS = ["claude-sonnet-5", "claude-sonnet-4-5", "claude-3-7-sonnet-latest"]


def get_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    env_path = os.path.join(B.REPO, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            m = re.match(r"\s*ANTHROPIC_API_KEY\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None


def call_claude(key, prompt):
    last_err = None
    for model in MODELS:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key,
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json=dict(model=model, max_tokens=4000,
                                    messages=[dict(role="user", content=prompt)]),
                          timeout=120)
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        last_err = f"{model}: {r.status_code} {r.text[:200]}"
    raise RuntimeError(f"Anthropic API failed: {last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run dir containing pool2.json")
    ap.add_argument("--n", type=int, default=12, help="suggestions to request")
    ap.add_argument("--train-end", default=None)
    args = ap.parse_args()

    key = get_key()
    if not key:
        print("NO_API_KEY: set ANTHROPIC_API_KEY in env or repo .env file")
        return 1

    run_dir = args.run if os.path.isabs(args.run) else os.path.join(B.OPT_DIR, args.run)
    os.chdir(run_dir)
    pool = json.load(open("pool2.json"))["pool"]
    space = json.load(open(os.path.join(B.OPT_DIR, "param_space.json")))["v7"]
    best = pool[:8]
    worst = pool[-8:] if len(pool) > 16 else []
    mode = best[0][1]["mode"]
    R = len(best[0][1]["regs"])

    prompt = f"""You are helping optimize parameters for a crypto trading strategy (SOL/USDT perp, 3-minute bars).
A candidate is a JSON object: {{"strategy":"v7","mode":"{mode}","regs":[<{R} parameter dicts, one per market regime (0=low volatility, ..., {R-1}=high)>]}}.

Parameter space (continuous params have [min,max]; menu params must use one of the listed options; flags are 0.0/1.0):
{json.dumps({k: v['range'] for k, v in space['continuous'].items()})}
menus: {json.dumps({k: v['options'] for k, v in space['menus'].items()})}
flags: {list(space['flags'])}

The best candidates found so far (score, then their regime parameter sets):
{json.dumps([(round(s, 4), c['regs']) for s, c, m in best], default=float)[:6000]}

{"Weak candidates for contrast: " + json.dumps([(round(s, 4), c['regs'][0]) for s, c, m in worst], default=float)[:2000] if worst else ""}

Propose {args.n} NEW candidate parameter sets that might outperform. Consider: what the best candidates have in common, unexplored corners of the ranges, interactions (e.g. shorter indicator lengths need wider thresholds), and per-regime logic (low-volatility regimes usually need more sensitive entries and can carry more leverage than high-volatility ones — mode '{mode}').
Reply with ONLY a JSON array of {args.n} candidate objects, exactly matching the schema above, all values inside the allowed ranges/options."""

    print("asking Claude for suggestions...")
    text = call_claude(key, prompt)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        print("could not parse AI reply")
        return 1
    cands = json.loads(m.group(0))
    print(f"got {len(cands)} suggestions; evaluating...")

    import optimizer2 as O
    O.load_g3()
    method_guess = json.load(open("best_config.json")).get("method", "vol3") \
        if os.path.exists("best_config.json") else "vol3"
    added = 0
    for c in cands:
        c["strategy"] = "v7"; c["mode"] = mode
        try:
            mres = O.eval3(c, method_guess, None, args.train_end)
        except Exception as e:
            print("  eval failed:", e); continue
        if O.feasible3(mres, mode):
            pool.append((mres["score"], c, mres))
            added += 1
            print(f"  feasible: score {mres['score']:.4f} eq {mres['eq']:.0f} dd {mres['maxdd']:.2f}")
        else:
            print("  infeasible" + (f" (score {mres['score']:.3f})" if mres else ""))
    pool.sort(key=lambda x: -x[0])
    d = json.load(open("pool2.json")); d["pool"] = pool[:300]
    json.dump(d, open("pool2.json", "w"), default=float)
    print(f"merged {added} AI candidates into the pool (they compete on equal terms).")
    print("Continue the search with optimize2_cli.py using the same --name to exploit them.")


if __name__ == "__main__":
    raise SystemExit(main())
