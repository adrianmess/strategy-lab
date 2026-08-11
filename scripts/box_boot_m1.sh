#!/bin/bash
# On-box boot for the m1sweep worker. Ships to ~/boot_m1.sh on each EC2 box.
# Direction comes from ~/m1_direction (contains "--reverse" or is absent).
J=$(( $(nproc) / 11 )); [ "$J" -lt 2 ] && J=2
REV=""
[ -f ~/m1_direction ] && REV="$(cat ~/m1_direction)"
tmux new-session -d -s keeper 'sleep infinity' 2>/dev/null || true
tmux has-session -t m1 2>/dev/null || tmux new-session -d -s m1 \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/m1sweep_0810/plan.json --jobs $J $REV 2>&1 | tee -a ~/worker.log"
