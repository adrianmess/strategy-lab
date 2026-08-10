#!/bin/bash
# One-shot EC2 box setup + launch for the m1sweep campaign.
# Run FROM THE MAC once the instance is up:
#   ./scripts/ec2_m1_boot.sh <box-ip>
# Assumes key ~/Downloads/gamut-key.pem and campaign m1sweep_0810 built.
set -euo pipefail
IP="$1"
PEM="$HOME/Downloads/gamut-key.pem"
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
RS="rsync -az -e \"$SSH\""

echo "== waiting for ssh =="
while ! nc -z -w 5 "$IP" 22 2>/dev/null; do sleep 10; done; sleep 15

echo "== system deps + venv =="
$SSH ubuntu@$IP 'sudo apt-get -qq update && sudo apt-get -qq install -y python3-venv >/dev/null;
  python3 -m venv ~/venv && ~/venv/bin/pip -q install --upgrade pip &&
  ~/venv/bin/pip -q install numpy pandas numba pyarrow requests python-dotenv;
  sudo loginctl enable-linger ubuntu;
  sudo timedatectl set-timezone America/Los_Angeles;
  ~/venv/bin/python3 -V'

echo "== code + data =="
eval $RS --exclude '.git' --exclude 'optimizer/runs' --exclude '*cache*' \
     --exclude 'dashboard/bt_detail' --exclude 'dashboard/backtests.js' \
     --exclude 'panel/jobs' --exclude 'chrome_user_data' \
     "$LAB/" ubuntu@$IP:strategy-lab/
$SSH ubuntu@$IP 'printf "window.BACKTESTS = [];" > strategy-lab/dashboard/backtests.js; mkdir -p strategy-lab/dashboard/bt_detail strategy-lab/optimizer/runs'

echo "== source run pools =="
eval $RS --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/m1sweep_0810/sources.txt") \
     --include '*/' --include 'pool2.json' --include 'best_config.json' \
     --include 'holdout_best_config.json' --exclude '*' \
     "$LAB/optimizer/runs/" ubuntu@$IP:strategy-lab/optimizer/runs/

echo "== start worker (tmux, jobs = nproc/11) =="
$SSH ubuntu@$IP 'sudo apt-get -qq install -y tmux >/dev/null;
  J=$(( $(nproc) / 11 )); [ "$J" -lt 2 ] && J=2;
  tmux new-session -d -s keeper "sleep infinity" 2>/dev/null || true;
  tmux new-session -d -s m1 ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/m1sweep_0810/plan.json --jobs $J 2>&1 | tee -a ~/worker.log";
  echo "worker started with jobs=$J"; tmux ls'

echo
echo "box ready. Start the sync loop on the Mac with:"
echo "  SYNC_SFX_START=2 nohup ./scripts/offload_sync.sh $PEM m1sweep_0810 $IP > /tmp/ec2_m1_sync.log 2>&1 &"
