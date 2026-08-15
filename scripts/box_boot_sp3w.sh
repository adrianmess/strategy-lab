#!/bin/bash
# On-box boot for the SPOT 3m merge sweep (campaign sp3w_0814).
# Direction from ~/m1_direction ("--reverse" or absent = forward).
tmux new-session -d -s keeper 'sleep infinity' 2>/dev/null || true
REV=""
[ -f "$HOME/m1_direction" ] && REV="$(cat "$HOME/m1_direction")"
tmux has-session -t s3w 2>/dev/null || tmux new-session -d -s s3w \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/sp3w_0814/plan.json --jobs 6 $REV 2>&1 | tee -a ~/worker_sp3w.log"
