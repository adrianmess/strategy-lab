#!/bin/bash
# Ship + launch the SOL 1m merge sweep (sol1w_0811) on the two EC2 boxes.
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
PEM="$HOME/Downloads/gamut-key.pem"
S="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15"
IPF="${SOL_IPF:-52.15.227.237}"
IPR="${SOL_IPR:-3.17.12.141}"

for IP in "$IPF" "$IPR"; do
  echo "== box $IP =="
  # source pools (includes pool2.json — merges need them; markers alone won't do)
  rsync -az -e "$S" --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/sol1w_0811/sources.txt") \
    --include='*/' --include='pool2.json' --include='best_config.json' \
    --include='holdout_best_config.json' --exclude='*' \
    "$LAB/optimizer/runs/" "ubuntu@$IP:strategy-lab/optimizer/runs/"
  $S "ubuntu@$IP" 'mkdir -p strategy-lab/optimizer/campaigns/sol1w_0811'
  rsync -az -e "$S" "$LAB/optimizer/campaigns/sol1w_0811/plan.json" \
    "ubuntu@$IP:strategy-lab/optimizer/campaigns/sol1w_0811/plan.json"
  $S "ubuntu@$IP" 'cd strategy-lab/optimizer/campaigns && ln -sfn sol1w_0811 gamut_sol1w_0811
    REV=""; [ -f ~/m1_direction ] && REV="$(cat ~/m1_direction)"
    J=$(( $(nproc) / 11 )); [ "$J" -lt 2 ] && J=2
    tmux kill-session -t s1w 2>/dev/null
    tmux new-session -d -s s1w ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/sol1w_0811/plan.json --jobs $J $REV 2>&1 | tee -a ~/worker_s1w.log"
    sleep 6; tmux ls; tail -2 ~/worker_s1w.log'
done
echo "SOL1W LAUNCHED"
