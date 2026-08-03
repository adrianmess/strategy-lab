#!/bin/bash
# Run ON THE MAC. Pulls completed runs + worker state back from the EC2 box
# every 5 minutes. Additive only (--ignore-existing): never overwrites local
# runs. Usage:  ./scripts/offload_sync.sh <EC2_IP> <PEM_PATH> [CAMPAIGN]
set -u
IP="$1"; PEM="$2"; CAMP="${3:-gamut_g0801_2122}"
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -i $PEM -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30"
while true; do
  rsync -az --ignore-existing -e "$SSH" \
    "ubuntu@$IP:strategy-lab/optimizer/runs/" "$LAB/optimizer/runs/" 2>/dev/null
  for cdir in "$LAB"/optimizer/campaigns/gamut_*/; do
    cn=$(basename "$cdir")
    rsync -az -e "$SSH" \
      "ubuntu@$IP:strategy-lab/optimizer/campaigns/$cn/worker_state.json" \
      "$cdir/worker_state.json" 2>/dev/null
  done
  # backtest entries are PUBLISHED on the box that ran them — pull its
  # backtests.js and merge new entries into the local Backtests page
  rsync -az -e "$SSH" \
    "ubuntu@$IP:strategy-lab/dashboard/backtests.js" /tmp/ec2_backtests.js \
    2>/dev/null && python3 "$LAB/scripts/merge_backtests.py" /tmp/ec2_backtests.js
  echo "[$(date '+%H:%M:%S')] sync ok — $(python3 -c "
import json;d=json.load(open('$LAB/optimizer/campaigns/$CAMP/worker_state.json'))
from collections import Counter;print(dict(Counter(v['status'] for v in d.values())))" 2>/dev/null || echo 'no state yet')"
  sleep 300
done
