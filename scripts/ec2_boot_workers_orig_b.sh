#!/bin/bash
# BOX D boot: REVERSE worker on gorig_mh12 (meet-in-the-middle).
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
# CORE BUDGET — size it from THIS box, never from whatever shipped in
# the repo bundle. See the header of ec2_size_cores.sh for the incident.
[ -x ~/ec2_size_cores.sh ] && ~/ec2_size_cores.sh

echo "{\"cores\": $(nproc)}" > ~/strategy-lab/optimizer/gamut_limits.json
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_gorig_mh12/plan.json --jobs $J --reverse --loop 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_workers_orig_b ran (jobs=$J)" >> ~/boot_workers.log
