#!/bin/bash
# Launch the HYPE 1m sweep: hyp1a_0811 on the two EC2 boxes (fwd/rev),
# hyp1b_0811 on main Mac (fwd) + mini (rev). Run from the Mac with bash.
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
PEM="$HOME/Downloads/gamut-key.pem"
S="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15"
IPF="${HYP1_IPF:-52.15.227.237}"   # forward box
IPR="${HYP1_IPR:-3.138.201.50}"    # reverse box
ONLY_BOXES="${ONLY_BOXES:-}"       # set to 1 to skip mini/Mac (already armed)

for IP in "$IPF" "$IPR"; do
  echo "== box $IP: sources + plan + boot =="
  rsync -az -e "$S" --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/hyp1a_0811/sources.txt") \
    --include='*/' --include='pool2.json' --include='best_config.json' \
    --include='holdout_best_config.json' --exclude='*' \
    "$LAB/optimizer/runs/" "ubuntu@$IP:strategy-lab/optimizer/runs/"
  $S "ubuntu@$IP" 'mkdir -p strategy-lab/optimizer/campaigns/hyp1a_0811'
  rsync -az -e "$S" "$LAB/optimizer/campaigns/hyp1a_0811/plan.json" \
    "ubuntu@$IP:strategy-lab/optimizer/campaigns/hyp1a_0811/plan.json"
  rsync -az -e "$S" "$LAB/scripts/box_boot_h1.sh" "ubuntu@$IP:boot_h1.sh"
  $S "ubuntu@$IP" 'chmod +x ~/boot_h1.sh
    cd strategy-lab/optimizer/campaigns && ln -sfn hyp1a_0811 gamut_hyp1a_0811
    ( crontab -l 2>/dev/null | grep -v boot_m1 | grep -v boot_h1; echo "@reboot sleep 40 && /home/ubuntu/boot_h1.sh" ) | crontab -
    tmux kill-session -t m1 2>/dev/null
    ~/boot_h1.sh; sleep 5; tmux ls; tail -2 ~/worker_h1.log'
done

[ -n "$ONLY_BOXES" ] && { echo "boxes done (ONLY_BOXES)"; exit 0; }

echo "== mini: sources + plan + reverse worker =="
rsync -az --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/hyp1b_0811/sources.txt") \
  --include='*/' --include='pool2.json' --include='best_config.json' \
  --include='holdout_best_config.json' --exclude='*' \
  "$LAB/optimizer/runs/" "admn@admns-Mac-mini.local:strategy-lab/optimizer/runs/"
ssh admn@admns-Mac-mini.local 'mkdir -p strategy-lab/optimizer/campaigns/hyp1b_0811'
rsync -az "$LAB/optimizer/campaigns/hyp1b_0811/plan.json" \
  "admn@admns-Mac-mini.local:strategy-lab/optimizer/campaigns/hyp1b_0811/plan.json"
ssh admn@admns-Mac-mini.local 'cd strategy-lab/optimizer/campaigns && ln -sfn hyp1b_0811 gamut_hyp1b_0811
  cd .. && nohup caffeinate -i ~/venv/bin/python3 gamut_worker.py --plan campaigns/hyp1b_0811/plan.json --jobs 1 --reverse >> ~/worker_h1.log 2>&1 &
  sleep 5; tail -2 ~/worker_h1.log'

echo "== main Mac: forward worker =="
cd "$LAB/optimizer" && nohup caffeinate -i python3 gamut_worker.py \
  --plan campaigns/hyp1b_0811/plan.json --jobs 1 > /tmp/hyp1b_forward.log 2>&1 &
sleep 5; tail -2 /tmp/hyp1b_forward.log
echo "ALL LAUNCHED"
