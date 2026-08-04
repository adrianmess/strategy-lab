#!/usr/bin/env python3
"""Merge backtest entries from an offload box's backtests.js into the local
dashboard/backtests.js (additive by entry name — local entries never touched).
Usage: python3 merge_backtests.py /tmp/ec2_backtests.js
"""
import json, os, sys

LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "dashboard", "backtests.js")


def load(path):
    """Tolerant load: parse the FIRST valid JSON array after '=' and ignore
    any trailing garbage (in-place rewrites have left stale tails before)."""
    txt = open(path).read()
    body = txt[txt.index("=") + 1:].lstrip()
    val, end = json.JSONDecoder().raw_decode(body)
    dirty = bool(body[end:].strip().lstrip(";").strip())
    return val, dirty


def main():
    from prune_backtests import prune_entry, split_entry   # keep file loadable
    remote_p = sys.argv[1]
    if not os.path.exists(remote_p):
        print("no remote file"); return
    local, dirty = load(LOCAL) if os.path.exists(LOCAL) else ([], False)
    if dirty:
        print("local backtests.js had trailing garbage — will rewrite clean")
    have = {e.get("name") for e in local}
    added = 0
    remote_entries, _ = load(remote_p)
    for e in remote_entries:
        if e.get("name") not in have:
            local.append(split_entry(prune_entry(e)))
            have.add(e.get("name"))
            added += 1
    # per-run payload copies (runs/<run>/bts/<entry>.json) arrive with the
    # runs/ sync and survive any backtests.js corruption — merge those too
    import glob
    runs_glob = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                             "optimizer", "runs", "*", "bts", "*.json")
    for p in glob.glob(runs_glob):
        nm = os.path.basename(p)[:-5]
        if nm not in have:
            try:
                local.append(split_entry(prune_entry(json.load(open(p)))))
                have.add(nm)
                added += 1
            except Exception:
                pass
    if added or dirty:
        tmp = LOCAL + ".tmp"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS=" + json.dumps(local) + ";")
        os.replace(tmp, LOCAL)
    print(f"merged {added} new backtest entries")


if __name__ == "__main__":
    main()
