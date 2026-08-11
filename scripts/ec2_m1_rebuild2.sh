#!/bin/bash
# Rebuild the m1sweep EC2 front on TWO half-size boxes (us-east-2b).
# Usage: ./scripts/ec2_m1_rebuild2.sh <ip_forward> <ip_reverse>
# Fixes vs ec2_m1_boot.sh: no eval (quoting-safe excludes), @reboot cron
# self-arm, per-box direction flag.
set -uo pipefail
IPF="$1"; IPR="$2"
PEM="$HOME/Downloads/gamut-key.pem"
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSHO=(-i "$PEM" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

boot_one() {  # $1=ip  $2=direction flag ("" or "--reverse")
  local IP="$1" REV="$2"
  echo "== [$IP] wait for ssh =="
  while ! nc -z -w 5 "$IP" 22 2>/dev/null; do sleep 10; done; sleep 15

  echo "== [$IP] deps + venv =="
  ssh "${SSHO[@]}" "ubuntu@$IP" 'sudo apt-get -qq update >/dev/null 2>&1; sudo apt-get -qq install -y python3-venv tmux >/dev/null 2>&1;
    [ -x ~/venv/bin/python3 ] || { python3 -m venv ~/venv && ~/venv/bin/pip -q install --upgrade pip && ~/venv/bin/pip -q install numpy pandas numba pyarrow requests python-dotenv; };
    sudo loginctl enable-linger ubuntu; sudo timedatectl set-timezone America/Los_Angeles; ~/venv/bin/python3 -V'

  echo "== [$IP] code + data =="
  rsync -az -e "ssh ${SSHO[*]}" \
      --exclude=.git --exclude=optimizer/runs --exclude=optimizer/cache \
      --exclude=cache --exclude=dashboard/bt_detail --exclude=dashboard/backtests.js \
      --exclude=panel/jobs --exclude=chrome_user_data \
      "$LAB/" "ubuntu@$IP:strategy-lab/"
  ssh "${SSHO[@]}" "ubuntu@$IP" 'printf "window.BACKTESTS = [];" > strategy-lab/dashboard/backtests.js; mkdir -p strategy-lab/dashboard/bt_detail strategy-lab/optimizer/runs'

  echo "== [$IP] source run pools =="
  rsync -az -e "ssh ${SSHO[*]}" \
      --files-from=<(sed 's|$|/|' "$LAB/optimizer/campaigns/m1sweep_0810/sources.txt") \
      --include='*/' --include='pool2.json' --include='best_config.json' \
      --include='holdout_best_config.json' --exclude='*' \
      "$LAB/optimizer/runs/" "ubuntu@$IP:strategy-lab/optimizer/runs/"

  echo "== [$IP] boot script + cron + start worker ($REV) =="
  ssh "${SSHO[@]}" "ubuntu@$IP" "cat > ~/boot_m1.sh <<EOF
#!/bin/bash
J=\\\$(( \\\$(nproc) / 11 )); [ \\\"\\\$J\\\" -lt 2 ] && J=2
tmux new-session -d -s keeper 'sleep infinity' 2>/dev/null || true
tmux has-session -t m1 2>/dev/null || tmux new-session -d -s m1 \\\". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/m1sweep_0810/plan.json --jobs \\\$J $REV 2>&1 | tee -a ~/worker.log\\\"
EOF
chmod +x ~/boot_m1.sh
( crontab -l 2>/dev/null | grep -v boot_m1; echo '@reboot sleep 40 && /home/ubuntu/boot_m1.sh' ) | crontab -
~/boot_m1.sh; sleep 3; tmux ls"
  echo "== [$IP] done =="
}

boot_one "$IPF" ""
boot_one "$IPR" "--reverse"
echo "ALL BOXES ARMED"
