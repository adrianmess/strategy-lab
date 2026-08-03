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
  rsync -az -e "$SSH" \
    "ubuntu@$IP:strategy-lab/optimizer/campaigns/$CAMP/worker_state.json" \
    "$LAB/optimizer/campaigns/$CAMP/worker_state.json" 2>/dev/null
  echo "[$(date '+%H:%M:%S')] sync ok — $(python3 -c "
import json;d=json.load(open('$LAB/optimizer/campaigns/$CAMP/worker_state.json'))
from collections import Counter;print(dict(Counter(v['status'] for v in d.values())))" 2>/dev/null || echo 'no state yet')"
  sleep 300
done
