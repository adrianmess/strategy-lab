#!/bin/bash
# Run ON the offload box. Re-publishes backtests that were lost while the
# old racy publisher was corrupting backtests.js: for every completed run
# whose backtest_flags.json references entries missing from BOTH
# backtests.js and the run's bts/ folder, re-run backtest_cli.
# Sequential + nice so the gamut workers keep priority.
set -u
cd "$HOME/strategy-lab/optimizer"
source "$HOME/venv/bin/activate"
python3 - <<'EOF' > /tmp/missing_bts.txt
import json, os
txt = open("../dashboard/backtests.js").read()
have = {e["name"] for e in json.JSONDecoder().raw_decode(
    txt[txt.index("=")+1:].lstrip())[0]}
out = []
for d in sorted(os.listdir("runs")):
    fp = os.path.join("runs", d, "backtest_flags.json")
    if not os.path.exists(fp):
        continue
    try:
        flags = json.load(open(fp))
    except Exception:
        continue
    for v in flags.values():
        nm, src = v.get("backtest"), v.get("source")
        if not nm or not src:
            continue
        if nm in have or os.path.exists(os.path.join("runs", d, "bts", nm + ".json")):
            continue
        if os.path.exists(os.path.join("runs", d, src)):
            out.append(f"{d}\t{src}\t{nm}")
print("\n".join(out))
EOF
n=$(grep -c . /tmp/missing_bts.txt || true)
echo "rebuilding $n missing backtest entries"
while IFS=$'\t' read -r run src nm; do
  [ -z "$run" ] && continue
  echo "[$(date +%H:%M:%S)] $nm"
  nice -n 15 python3 backtest_cli.py --config "runs/$run/$src" --name "$nm" \
    --gap-mode skip_contaminated > /dev/null 2>&1 || echo "  FAILED $nm"
done < /tmp/missing_bts.txt
echo "rebuild finished"
