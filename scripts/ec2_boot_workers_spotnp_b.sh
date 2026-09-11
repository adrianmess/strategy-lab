#!/bin/bash
# BOX B boot for gspot_newpairs: REVERSE worker (meet-in-the-middle).
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
# reset the live core budget to this machine's width — the AMI's stale
# {"cores":12} otherwise throttles every fleet replacement (see box A)
echo "{\"cores\": $(nproc)}" > ~/strategy-lab/optimizer/gamut_limits.json
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_gspot_newpairs/plan.json --jobs $J --reverse 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_workers_spotnp_b ran (jobs=$J)" >> ~/boot_workers.log
