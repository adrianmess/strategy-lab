#!/usr/bin/env python3
"""AI parameter-space generation (per pair, per timeframe).

Adrian's rule: for the INDICATOR-SIGNAL parameters (MACD thresholds, RSI
levels, BB %B levels, EMA/trend z-thresholds, cooldown %-triggers) mine the
ENTIRE run history — every surviving config that actually traded — and set
each range to [min value used, max value used] (±10% pad). Risk/exit
parameters (SL, TP, position durations, leverage, cooldown periods) are
Adrian's to set and are NEVER touched.

If an Anthropic key is available (env/.env, same as ai_advisor), the mined
bounds + pair volatility stats are handed to the LLM for a sanity pass — it
may widen/narrow within reason and must return JSON; on any failure the
mined bounds stand as-is. Provenance is recorded either way.

Usage:
  python3 gen_ai_spaces.py --space btc_3m            # one space -> .ai variant
  python3 gen_ai_spaces.py --all                     # every pair x tf
  python3 gen_ai_spaces.py --space btc_3m --no-llm   # mined bounds only
"""
import _bootstrap as B
import argparse, copy, json, os, re
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
SPACES = os.path.join(HERE, "param_spaces")
VARIANTS = os.path.join(SPACES, "variants")

# indicator-signal params the AI may set, per family (candidate-dict keys
# and the space keys they map to). Everything else is user territory.
AI_KEYS = {
    "v7":      {"rsiValLong": "rsiValLong", "rsiValShort": "rsiValShort",
                "bbValLong": "bbValLong", "bbValShort": "bbValShort",
                "zL": "zL", "zS": "zS", "zXS": "zXS", "zXLmax": "zXLmax",
                "cdPctLong": "cdPctLong", "cdPctShort": "cdPctShort",
                "xCdPctShort": "xCdPctShort"},
    "prime7":  {"rsiValLong": "rsiValLong", "rsiValShort": "rsiValShort",
                "bbValLong": "bbValLong", "bbValShort": "bbValShort",
                "zL": "zL", "zS": "zS",
                "cdPctLong": "cdPctLong", "cdPctShort": "cdPctShort"},
    "prime":   {"rsiL": "rsiValLong", "rsiS": "rsiValShort",
                "bbL": "bbL", "bbS": "bbS", "zL": "zL", "zS": "zS",
                "cdPL": "cdPctLong", "cdPS": "cdPctShort"},
    "v6":      {"zL": "zL", "zS": "zS", "zXS": "zXS", "zXLmax": "zXLmax"},
    "macdx":   {"mMinS": "mMinS", "mMaxL": "mMaxL",
                "cdPL": "cdPL", "cdPS": "cdPS"},
    "scalpx":  {"rsiOB": "rsiOB", "rsiOS": "rsiOS"},
    "scalpx2": {"rsiOB": "rsiOB", "rsiOS": "rsiOS"},
    "rocx":    {},   # rocx has no indicator thresholds outside user params
}


def _cand_values(cand, key):
    """All values a candidate used for a param (per-regime lists, regs dicts,
    cadapt ends — everything it ACTUALLY traded with)."""
    out = []
    if "regs" in (cand or {}):
        for reg in cand["regs"]:
            if key in reg:
                out.append(float(reg[key]))
    elif isinstance((cand or {}).get(key), list):
        out += [float(x) for x in cand[key]]
    elif key in (cand or {}):
        try:
            out.append(float(cand[key]))
        except (TypeError, ValueError):
            pass
    return [v for v in out if np.isfinite(v) and v > -90]   # skip sentinels


def mine(coin, fam):
    """min/max of every value USED by surviving configs of this family —
    preferring the pair's own runs, falling back to the whole history."""
    vals_pair, vals_all = {}, {}
    keymap = AI_KEYS.get(fam, {})
    if not keymap:
        return {}, "no AI-managed params for this family"
    for d in os.listdir(RUNS):
        for fn in ("best_config.json", "holdout_best_config.json"):
            p = os.path.join(RUNS, d, fn)
            if not os.path.exists(p):
                continue
            try:
                b = json.load(open(p))
            except Exception:
                continue
            strat = b.get("strategy") or (b.get("cand") or {}).get("strategy")
            if strat != fam or not b.get("cand"):
                continue
            h = ((b.get("holdout_best") or {}).get("holdout")
                 or b.get("holdout") or {})
            if not h or h.get("liq"):
                continue           # only configs that survived honestly
            tgt = vals_pair if (b.get("pair") or "SOL_USDT").split("_")[0].lower() == coin \
                else vals_all
            for ck in keymap:
                for v in _cand_values(b["cand"], ck):
                    tgt.setdefault(ck, []).append(v)
    src = "pair-specific runs"
    vals = {k: v for k, v in vals_pair.items() if len(v) >= 5}
    for k, v in vals_all.items():          # fill gaps from the full history
        if k not in vals:
            vals[k] = v
            src = "pair runs + full history"
    ranges = {}
    for ck, v in vals.items():
        lo, hi = min(v), max(v)
        pad = 0.1 * (hi - lo) if hi > lo else abs(lo) * 0.1 + 1e-6
        ranges[keymap[ck]] = [round(lo - pad, 6), round(hi + pad, 6),
                              len(v)]      # third item = evidence count
    return ranges, src


def llm_refine(space_name, fam, mined, note):
    import sys
    sys.path.insert(0, HERE)
    from ai_advisor import get_key, call_claude
    key = get_key()
    if not key:
        return None, "no API key — mined bounds used as-is"
    coin, tf = space_name.split("_")
    prompt = (
        "You are tuning OPTIMIZER SEARCH RANGES (not trading values) for a "
        f"crypto strategy family '{fam}' on {coin.upper()}/USDT, {tf} chart "
        "bars. Below are ranges mined from every surviving config's actually-"
        "used values [min_used-10%pad, max_used+10%pad, n_evidence]. "
        "Adjust ONLY if clearly warranted (tiny evidence, degenerate width, "
        "or bounds that make no sense for the indicator's natural scale — "
        "e.g. RSI levels live in 0..100, BB %B roughly -0.5..1.5, z-scores "
        "roughly -6..6). Keep changes conservative. Return ONLY JSON: "
        '{"param": [lo, hi], ...} for params you would change; {} if none.\n\n'
        + json.dumps(mined) + "\nmined from: " + note)
    try:
        txt = call_claude(key, prompt)
        m = re.search(r"\{[^{}]*(\{[^{}]*\}[^{}]*)*\}", txt, re.S)
        adj = json.loads(m.group(0)) if m else {}
        good = {k: [float(v[0]), float(v[1])] for k, v in adj.items()
                if k in mined and isinstance(v, (list, tuple)) and len(v) >= 2
                and float(v[0]) < float(v[1])}
        return good, f"LLM reviewed; adjusted {sorted(good)}" if good \
            else (good, "LLM reviewed; no changes")
    except Exception as e:
        return None, f"LLM unavailable ({e}) — mined bounds used as-is"


def generate(space_name, use_llm=True):
    src_path = (os.path.join(HERE, "param_space.json")
                if space_name == "default"
                else os.path.join(SPACES, f"{space_name}.json"))
    if not os.path.exists(src_path):
        raise SystemExit(f"no space file {src_path}")
    coin = "sol" if space_name == "default" else space_name.split("_")[0]
    sp = copy.deepcopy(json.load(open(src_path)))
    prov = {}
    for fam in list(AI_KEYS):
        mined, note = mine(coin, fam)
        if not mined:
            continue
        adj, lnote = (None, "LLM skipped")
        if use_llm:
            adj, lnote = llm_refine(space_name if space_name != "default"
                                    else "sol_3m", fam, mined, note)
        for var in (fam, f"{fam}@spot"):
            cont = (sp.get(var) or {}).get("continuous") or {}
            for pk, (lo, hi, n) in mined.items():
                if pk in cont and "range" in cont[pk]:
                    if adj and pk in adj:
                        lo, hi = adj[pk]
                    cont[pk]["range"] = [lo, hi]
        prov[fam] = dict(mined={k: v[:2] for k, v in mined.items()},
                         evidence={k: v[2] for k, v in mined.items()},
                         source=note, llm=lnote)
    sp["_meta"] = dict(sp.get("_meta") or {}, variant="ai",
                       ai_provenance=prov,
                       ai_generated=__import__("time").strftime("%Y-%m-%d %H:%M"),
                       note="AI variant: indicator-signal ranges from mined "
                            "min/max of actually-used values (LLM sanity pass "
                            "when key available). SL/TP/durations/leverage "
                            "untouched — those are Adrian's.")
    os.makedirs(VARIANTS, exist_ok=True)
    out = os.path.join(VARIANTS, f"{space_name}.ai.json")
    json.dump(sp, open(out, "w"), indent=1)
    fams = sorted(prov)
    print(f"{space_name}: AI variant written ({', '.join(fams)}) -> {out}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default=None, help="e.g. btc_3m, or 'default'")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    names = ([a.space] if a.space else []) if not a.all else \
        ["default"] + sorted(f[:-5] for f in os.listdir(SPACES)
                             if re.fullmatch(r"[a-z]{2,6}_[135]m\.json", f))
    if not names:
        raise SystemExit("give --space <name> or --all")
    for n in names:
        generate(n, use_llm=not a.no_llm)
