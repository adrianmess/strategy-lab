#!/bin/bash
# Runs ON an EC2 box: switch it from the (finished) 1m spot gamut to the 3m
# one. Installs the plan, clears the SPOT engine caches so the searches use
# the freshly-synced candle data (caches are keyed on indicator settings, NOT
# on the data), and starts the worker. Direction from ~/m1_direction.
set -u
cd "$HOME/strategy-lab/optimizer" || exit 1

tmux kill-session -t spg 2>/dev/null
pkill -f "gamut_worker.py --plan" 2>/dev/null
sleep 2

mkdir -p campaigns/spotg3m_0811
cp /tmp/spotg3m_plan.json campaigns/spotg3m_0811/plan.json
( cd campaigns && ln -sfn spotg3m_0811 gamut_spotg3m_0811 )

n=$(find cache -path "*spotdata*" -name "*.pkl" 2>/dev/null | wc -l)
find cache -path "*spotdata*" -name "*.pkl" -delete 2>/dev/null
echo "plan installed; cleared $n stale spot caches (rebuild on new data)"

REV=""
[ -f "$HOME/m1_direction" ] && REV="$(cat "$HOME/m1_direction")"
tmux new-session -d -s keeper 'sleep infinity' 2>/dev/null || true
tmux new-session -d -s spg ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/spotg3m_0811/plan.json --jobs 6 $REV 2>&1 | tee -a ~/worker_spg3m.log"
sleep 6
tmux ls
tail -2 "$HOME/worker_spg3m.log" 2>/dev/null
