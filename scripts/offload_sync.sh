#!/bin/bash
# Run ON THE MAC. Multi-box sync every 5 minutes:
#  - pull completed runs from every box (additive, never overwrites local)
#  - PUSH done-markers to every box (tightens the meet-in-the-middle point
#    and prevents re-running specs another box finished)
#  - pull worker_state from each box (worker_state.json / worker_state_b.json)
#  - pull each box's backtests.js + merge new entries (pruned+split)
# Usage: ./scripts/offload_sync.sh <PEM> <CAMPAIGN> <IP_A> [IP_B]
set -u
PEM="$1"; CAMP="$2"; shift 2
IPS=("$@")
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o ServerAliveInterval=30"
while true; do
  idx=0
  for IP in "${IPS[@]}"; do
    sfx=""; [ $idx -gt 0 ] && sfx="_b"
    rsync -az --ignore-existing --timeout=60 -e "$SSH" \
      "ubuntu@$IP:strategy-lab/optimizer/runs/" "$LAB/optimizer/runs/" 2>/dev/null
    rsync -az --timeout=60 --include='*/' --include='best_config.json' \
      --include='no_survivor.json' --exclude='*' \
      -e "$SSH" "$LAB/optimizer/runs/" "ubuntu@$IP:strategy-lab/optimizer/runs/" 2>/dev/null
    # keep worker code current on the boxes (applies at their next restart)
    rsync -az --timeout=30 -e "$SSH" "$LAB/optimizer/gamut_worker.py" \
      "ubuntu@$IP:strategy-lab/optimizer/gamut_worker.py" 2>/dev/null
    for cdir in "$LAB"/optimizer/campaigns/gamut_*/; do
      cn=$(basename "$cdir")
      rsync -az --timeout=30 -e "$SSH" \
        "ubuntu@$IP:strategy-lab/optimizer/campaigns/$cn/worker_state.json" \
        "$cdir/worker_state$sfx.json" 2>/dev/null
    done
    rsync -az --timeout=60 -e "$SSH" \
      "ubuntu@$IP:strategy-lab/dashboard/backtests.js" "/tmp/ec2_backtests$sfx.js" \
      2>/dev/null && python3 "$LAB/scripts/merge_backtests.py" "/tmp/ec2_backtests$sfx.js"
    idx=$((idx+1))
  done
  echo "[$(date '+%H:%M:%S')] sync ok — $(python3 -c "
import json,collections,glob
c=collections.Counter()
for f in glob.glob('$LAB/optimizer/campaigns/gamut_$CAMP/worker_state*.json'):
    try:
        for v in json.load(open(f)).values(): c[v['status']]+=1
    except Exception: pass
print(dict(c))" 2>/dev/null || echo 'no state yet')"
  sleep 300
done
