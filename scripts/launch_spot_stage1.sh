#!/bin/bash
# Launch the SPOT stage-1 gamuts: spotg1m_0811 on the EC2 boxes (fwd/rev),
# spotg3m_0811 on main Mac (fwd, procs<=12) + mini (rev, procs<=10).
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
PEM="$HOME/Downloads/gamut-key.pem"
S="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15"
IPF="${SPOT_IPF:-52.15.227.237}"
IPR="${SPOT_IPR:-3.17.12.141}"
DATA="adaptive_trader/research/data"

for IP in "$IPF" "$IPR"; do
  echo "== box $IP =="
  rsync -az -e "$S" "$LAB/optimizer/gamut_worker.py" "ubuntu@$IP:strategy-lab/optimizer/gamut_worker.py"
  rsync -az -e "$S" "$LAB/$DATA/hype_spot_1min.parquet" "$LAB/$DATA/hype_spot_3min.parquet" \
    "ubuntu@$IP:strategy-lab/$DATA/"
  $S "ubuntu@$IP" 'mkdir -p strategy-lab/optimizer/campaigns/spotg1m_0811'
  rsync -az -e "$S" "$LAB/optimizer/campaigns/spotg1m_0811/plan.json" \
    "ubuntu@$IP:strategy-lab/optimizer/campaigns/spotg1m_0811/plan.json"
  $S "ubuntu@$IP" 'cd strategy-lab/optimizer/campaigns && ln -sfn spotg1m_0811 gamut_spotg1m_0811
    REV=""; [ -f ~/m1_direction ] && REV="$(cat ~/m1_direction)"
    J=$(( $(nproc) / 11 )); [ "$J" -lt 2 ] && J=2
    tmux kill-session -t spg 2>/dev/null
    tmux new-session -d -s spg ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/spotg1m_0811/plan.json --jobs $J $REV 2>&1 | tee -a ~/worker_spg.log"
    sleep 6; tail -2 ~/worker_spg.log'
done

echo "== mini (rev, procs<=10) =="
rsync -az "$LAB/optimizer/gamut_worker.py" admn@admns-Mac-mini.local:strategy-lab/optimizer/gamut_worker.py
rsync -az "$LAB/$DATA/hype_spot_1min.parquet" "$LAB/$DATA/hype_spot_3min.parquet" \
  "admn@admns-Mac-mini.local:strategy-lab/$DATA/"
ssh admn@admns-Mac-mini.local 'mkdir -p strategy-lab/optimizer/campaigns/spotg3m_0811'
rsync -az "$LAB/optimizer/campaigns/spotg3m_0811/plan.json" \
  "admn@admns-Mac-mini.local:strategy-lab/optimizer/campaigns/spotg3m_0811/plan.json"
ssh admn@admns-Mac-mini.local 'cd strategy-lab/optimizer/campaigns && ln -sfn spotg3m_0811 gamut_spotg3m_0811
  cd .. && nohup caffeinate -i ~/venv/bin/python3 gamut_worker.py --plan campaigns/spotg3m_0811/plan.json --jobs 1 --reverse --procs-cap 10 >> ~/worker_spg.log 2>&1 &
  sleep 5; tail -1 ~/worker_spg.log'

echo "== main Mac (fwd, procs<=12) =="
cd "$LAB/optimizer" && nohup caffeinate -i python3 gamut_worker.py \
  --plan campaigns/spotg3m_0811/plan.json --jobs 1 --procs-cap 12 > /tmp/spotg3m_forward.log 2>&1 &
sleep 5; tail -1 /tmp/spotg3m_forward.log
echo "SPOT STAGE 1 LAUNCHED"
