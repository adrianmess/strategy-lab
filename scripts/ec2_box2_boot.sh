#!/bin/bash
# Bootstrap + arm m1sweep box 2 (reverse direction). Run from the Mac.
set -uo pipefail
IP="${1:-18.222.172.101}"
PEM="$HOME/Downloads/gamut-key.pem"
LAB="$(cd "$(dirname "$0")/.." && pwd)"
S="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

echo "== deps + venv =="
$S ubuntu@$IP 'sudo apt-get -qq update >/dev/null 2>&1; sudo apt-get -qq install -y python3-venv tmux >/dev/null 2>&1;
  [ -x ~/venv/bin/python3 ] || { python3 -m venv ~/venv && ~/venv/bin/pip -q install --upgrade pip && ~/venv/bin/pip -q install numpy pandas numba pyarrow requests python-dotenv; };
  sudo loginctl enable-linger ubuntu; sudo timedatectl set-timezone America/Los_Angeles; ~/venv/bin/python3 -V'

echo "== code + data =="
rsync -az -e "$S" --exclude=.git --exclude=optimizer/runs --exclude=optimizer/cache \
  --exclude=cache --exclude=dashboard/bt_detail --exclude=dashboard/backtests.js \
  --exclude=panel/jobs --exclude=chrome_user_data "$LAB/" ubuntu@$IP:strategy-lab/
$S ubuntu@$IP 'printf "window.BACKTESTS = [];" > strategy-lab/dashboard/backtests.js; mkdir -p strategy-lab/dashboard/bt_detail strategy-lab/optimizer/runs'

echo "== source pools =="
rsync -az -e "$S" --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/m1sweep_0810/sources.txt") \
  --include='*/' --include='pool2.json' --include='best_config.json' \
  --include='holdout_best_config.json' --exclude='*' \
  "$LAB/optimizer/runs/" ubuntu@$IP:strategy-lab/optimizer/runs/

echo "== boot (reverse) =="
scp -i "$PEM" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
  "$LAB/scripts/box_boot_m1.sh" ubuntu@$IP:boot_m1.sh
scp -i "$PEM" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
  "$LAB/scripts/box_fix_m1.sh" ubuntu@$IP:fix_m1.sh
$S ubuntu@$IP 'echo "--reverse" > ~/m1_direction; bash ~/fix_m1.sh'
echo "BOX2 DONE rc=$?"
