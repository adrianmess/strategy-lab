#!/usr/bin/env python3
"""Generate the PROVENANCE STORY of a run (routers especially): every
optimization, combine, router build, refine and walk-forward that led to it,
ending with a plain-terms summary of what actually trades.

Facts are walked deterministically from launch.json / best_config.json /
walkforward.json provenance. If an Anthropic key is configured (env or .env,
same as the AI advisor) the narrative is written by the LLM from those facts
ONLY; otherwise a deterministic template renders them. Output is saved to
runs/<name>/story.md.

Usage:  python3 gen_story.py --run camp_c4_m_spot_vol3 [--no-llm]
"""
import _bootstrap as B
import argparse, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
AT = os.path.join(os.path.dirname(HERE), "adaptive_trader")


def _load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def launch_chain(run, seen=None):
    seen = seen or set()
    if run in seen:
        return []
    seen.add(run)
    j = _load(os.path.join(RUNS, run, "launch.json"))
    if not j:
        return [dict(run=run, note="pre-provenance run (no launch record)")]
    out = []
    for e in (j if isinstance(j, list) else [j]):
        keep = dict(re.findall(
            r"--(strategy|mode|method|algo|total|holdout-days|holdout-before|"
            r"holdout-between|holdout-outside|train-end|train-start|lockbox|"
            r"scoring|symbol|tf|max-hold-days|merge-mode)\s+(\S+)", e.get("cmd", "")))
        if "--lev-stops" in e.get("cmd", ""):
            keep["lev-stops"] = "on"
        rec = dict(run=run, at=e.get("at"), settings=keep)
        rf = [r.split("/")[-1] for r in (e.get("resume_from") or [])]
        if rf:
            rec["combined_from"] = rf
            rec["merge_mode"] = e.get("merge_mode")
            rec["parents"] = [launch_chain(r, seen) for r in rf]
        out.append(rec)
    return out


def comp_metrics(run):
    b = _load(os.path.join(RUNS, run, "best_config.json")) or {}
    h = ((b.get("holdout_best") or {}).get("holdout") or b.get("holdout") or {})
    m = {}
    if h and h.get("growth") is not None:
        m["holdout_pct_mo"] = round(100 * (2.718281828 ** h["growth"] - 1), 1)
        m["holdout_dd_pct"] = round(100 * (h.get("maxdd") or 0))
    m["pair"] = b.get("pair", "SOL_USDT")
    m["timeframe"] = b.get("timeframe", "3m")
    m["strategy"] = b.get("strategy")
    return m


def gather(run):
    b = _load(os.path.join(RUNS, run, "best_config.json"))
    if not b:
        sys.exit(f"run '{run}' not found")
    strat = b.get("strategy")
    cand = b.get("cand") or {}
    facts = dict(run=run, strategy=strat, mode=b.get("mode"),
                 method=b.get("method"), pair=b.get("pair", "SOL_USDT"),
                 timeframe=b.get("timeframe", "3m"),
                 market_data=b.get("market_data", "perp"),
                 generated=b.get("generated"), evaluated=b.get("evaluated"),
                 refined=bool(b.get("refined")))
    wf = _load(os.path.join(RUNS, run, "walkforward.json"))
    if wf:
        facts["walkforward"] = dict(verdict=wf.get("verdict"),
                                    oos_pct_mo=wf.get("oos_pct_mo"),
                                    dd_pct=round(100 * (wf.get("maxdd") or 0)),
                                    folds=wf.get("folds"))
    if strat in ("metax", "metax2") and cand.get("components"):
        assign = cand.get("assign") or []
        assigned = {a for a in assign if a is not None and a >= 0}
        facts["bucket_assign"] = assign
        facts["buckets"] = cand.get("buckets") or b.get("method")
        facts["components"] = []
        for k, c in enumerate(cand["components"]):
            facts["components"].append(dict(
                index=k, run=c.get("run"), strategy=c.get("strategy"),
                assigned=(k in assigned),
                pair=c.get("pair"), timeframe=c.get("timeframe"),
                metrics=comp_metrics(c.get("run") or ""),
                history=launch_chain(c.get("run") or "")))
    elif strat == "metax" and cand.get("stack"):
        facts["stack_sources"] = [s.get("run") for s in cand.get("sources", [])]
        facts["current_pick"] = cand.get("current")
    elif strat == "pairx":
        facts["current_per_pair"] = cand.get("current")
    else:
        facts["history"] = launch_chain(run)
    # adopted into a live config?
    for f in os.listdir(AT):
        if f.startswith("config") and f.endswith(".json"):
            c = _load(os.path.join(AT, f)) or {}
            src = (c.get("adopted_from") or {}).get("source", "")
            if f"/runs/{run}/" in src:
                facts["adopted_into"] = dict(
                    config=f, at=(c.get("adopted_from") or {}).get("at"),
                    dry_run=c.get("dry_run", True))
                # the LIVE config's assignment can differ from the run's
                # original (the 42-day re-assignment cadence updates it)
                la = (c.get("candidate") or {}).get("assign")
                if la is not None:
                    facts["live_current_assign"] = la
    return facts


def deterministic_story(f):
    L = [f"# Story of {f['run']}", ""]
    L.append(f"**What it is:** {f['strategy']} · {f['mode']} · "
             f"{f.get('method')} · {f['pair']} {f['timeframe']} "
             f"({f['market_data']} candles), built {f.get('generated')}.")
    if f.get("components"):
        L.append("\n## Components")
        for c in f["components"]:
            m = c.get("metrics") or {}
            tag = "● assigned" if c["assigned"] else "○ mined, unassigned"
            extra = f" — holdout {m.get('holdout_pct_mo', '?')}%/mo" \
                if m.get("holdout_pct_mo") is not None else ""
            L.append(f"- {tag}: **{c['run']}** ({c['strategy']}"
                     + (f", {c.get('pair')} {c.get('timeframe')}" if c.get('pair') else "")
                     + f"){extra}")
            for h in (c.get("history") or []):
                if h.get("note"):
                    L.append(f"    - {h['note']}")
                    continue
                st = h.get("settings", {})
                L.append(f"    - [{h.get('at')}] " + " ".join(
                    f"{k}={v}" for k, v in st.items()))
                if h.get("combined_from"):
                    L.append(f"      - COMBINED ({h.get('merge_mode')}) from: "
                             + ", ".join(h["combined_from"]))
    if f.get("history"):
        L.append("\n## Launch history")
        for h in f["history"]:
            st = h.get("settings", {})
            L.append(f"- [{h.get('at')}] " + " ".join(f"{k}={v}" for k, v in st.items()))
            if h.get("combined_from"):
                L.append(f"  - COMBINED ({h.get('merge_mode')}) from: "
                         + ", ".join(h["combined_from"]))
    if f.get("walkforward"):
        w = f["walkforward"]
        L.append(f"\n**Honest gate:** walk-forward {w['verdict']} — chained OOS "
                 f"{w.get('oos_pct_mo', 0):+.1f}%/mo, dd {w.get('dd_pct')}%, "
                 f"{w.get('folds')} folds.")
    if f.get("adopted_into"):
        a = f["adopted_into"]
        L.append(f"\n**Adopted:** into `{a['config']}` at {a['at']} "
                 f"({'dry-run' if a.get('dry_run') else 'LIVE'}).")
    if f.get("components") and f.get("bucket_assign"):
        names = {c["index"]: c for c in f["components"]}
        plain = []
        use_assign = f.get("live_current_assign") or f["bucket_assign"]
        which = ("current LIVE assignment (42d re-assignment cadence)"
                 if f.get("live_current_assign") else "original assignment")
        labels = ["low-vol", "mid-vol", "high-vol"] if len(use_assign) == 3 \
            else [f"bucket {i}" for i in range(len(use_assign))]
        for i, a in enumerate(use_assign):
            if a is None or a < 0:
                plain.append(f"{labels[i]}: flat")
            else:
                c = names.get(a, {})
                plain.append(f"{labels[i]}: {c.get('strategy')} ({c.get('run', '')[:32]})")
        L.append(f"\n**In plain terms** ({which}): " + " · ".join(plain) + ".")
    return "\n".join(L)


def llm_story(facts):
    sys.path.insert(0, HERE)
    from ai_advisor import get_key, call_claude
    key = get_key()
    if not key:
        return None, "no API key"
    prompt = (
        "Write the PROVENANCE STORY of a crypto trading configuration from "
        "the facts below. Format: markdown. Structure: numbered steps in "
        "chronological order (individual strategy optimizations -> any "
        "combines/merges -> router build with bucket assignment -> "
        "refine/walk-forward -> adoption), then end with a section starting "
        "exactly '**In plain terms:**' that says in 1-3 sentences what "
        "actually trades when (which strategy in which market regime). "
        "Rules: use ONLY these facts, never invent numbers or runs; keep it "
        "under 350 words; mention the honest walk-forward verdict; if a "
        "component predates launch tracking, say so.\n\nFACTS:\n"
        + json.dumps(facts, indent=1))
    try:
        return call_claude(key, prompt), "llm"
    except Exception as e:
        return None, f"LLM failed: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    run = os.path.basename(a.run.rstrip("/"))
    facts = gather(run)
    story, how = (None, "skipped")
    if not a.no_llm:
        story, how = llm_story(facts)
    if not story:
        story = deterministic_story(facts)
        how = f"deterministic ({how})"
    story += (f"\n\n---\n*generated {time.strftime('%Y-%m-%d %H:%M')} "
              f"({how}) — regenerate after extending/refining/re-adopting*")
    out = os.path.join(RUNS, run, "story.md")
    open(out, "w").write(story)
    print(f"story written -> {out} [{how}]")


if __name__ == "__main__":
    main()
