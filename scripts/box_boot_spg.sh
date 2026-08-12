#!/bin/bash
# On-box boot for the SPOT 1m gamut (campaign spotg1m_0811).
# Direction from ~/m1_direction ("--reverse" or absent = forward).
J=$(( $(nproc) / 11 )); [ "$J" -lt 2 ] && J=2
REV=""
[ -f ~/m1_direction ] && REV="$(cat ~/m1_direction)"
tmux new-session -d -s keeper 'sleep infinity' 2>/dev/null || true
tmux has-session -t spg 2>/dev/null || tmux new-session -d -s spg \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/spotg1m_0811/plan.json --jobs $J $REV 2>&1 | tee -a ~/worker_spg.log"
