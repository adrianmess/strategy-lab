#!/bin/bash
# Chain the SOL stage-1 gamuts behind the running HYPE 1m sweeps.
# Boxes: solg1m_0811 after hyp1a; Macs: solg3m_0811 after hyp1b.
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
PEM="$HOME/Downloads/gamut-key.pem"
S="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15"
IPF="${HYP1_IPF:-52.15.227.237}"
IPR="${HYP1_IPR:-3.138.201.50}"

for IP in "$IPF" "$IPR"; do
  echo "== box $IP: solg1m plan + chained session =="
  $S "ubuntu@$IP" 'mkdir -p strategy-lab/optimizer/campaigns/solg1m_0811'
  rsync -az -e "$S" "$LAB/optimizer/campaigns/solg1m_0811/plan.json" \
    "ubuntu@$IP:strategy-lab/optimizer/campaigns/solg1m_0811/plan.json"
  $S "ubuntu@$IP" 'cd strategy-lab/optimizer/campaigns && ln -sfn solg1m_0811 gamut_solg1m_0811
    REV=""; [ -f ~/m1_direction ] && REV="$(cat ~/m1_direction)"
    tmux has-session -t sg 2>/dev/null || tmux new-session -d -s sg \
      ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && while pgrep -f \"campaigns/hyp1[a]\" >/dev/null; do sleep 60; done; python3 gamut_worker.py --plan campaigns/solg1m_0811/plan.json --jobs 8 $REV 2>&1 | tee -a ~/worker_sg.log"
    tmux ls'
done

echo "== mini: solg3m plan + chained reverse worker =="
ssh admn@admns-Mac-mini.local 'mkdir -p strategy-lab/optimizer/campaigns/solg3m_0811'
rsync -az "$LAB/optimizer/campaigns/solg3m_0811/plan.json" \
  "admn@admns-Mac-mini.local:strategy-lab/optimizer/campaigns/solg3m_0811/plan.json"
ssh admn@admns-Mac-mini.local 'cd strategy-lab/optimizer/campaigns && ln -sfn solg3m_0811 gamut_solg3m_0811
  cd .. && nohup bash -c "while pgrep -f \"campaigns/hyp1[b]\" >/dev/null; do sleep 60; done; exec caffeinate -i $HOME/venv/bin/python3 gamut_worker.py --plan campaigns/solg3m_0811/plan.json --jobs 1 --reverse" >> ~/worker_sg.log 2>&1 &
  echo mini-chained'

echo "== main Mac: chained forward worker =="
cd "$LAB/optimizer" && nohup bash -c 'while pgrep -f "campaigns/hyp1[b]" >/dev/null; do sleep 60; done; exec caffeinate -i python3 gamut_worker.py --plan campaigns/solg3m_0811/plan.json --jobs 1' > /tmp/solg3m_forward.log 2>&1 &
echo "ALL CHAINED"
