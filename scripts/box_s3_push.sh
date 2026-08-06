#!/bin/bash
# Runs ON a box via cron (*/5). Pushes results to S3 so nothing depends on
# the box staying alive or reachable. Instance role provides credentials.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
B="s3://gamut-sync-637309463295"
R=us-east-2
cd "$HOME/strategy-lab" || exit 0
aws s3 sync optimizer/runs "$B/runs" --region $R --size-only --exclude "*_backtest_tmp*" >/dev/null 2>&1
for c in optimizer/campaigns/gamut_*/; do
  [ -f "$c/worker_state.json" ] && aws s3 cp "$c/worker_state.json" \
    "$B/state/$(basename "$c")/worker_state_$(hostname).json" --region $R >/dev/null 2>&1
done
aws s3 cp dashboard/backtests.js "$B/backtests/backtests_$(hostname).js" --region $R >/dev/null 2>&1
aws s3 cp "$HOME/strategy-lab/optimizer/gamut_worker.py" "$B/code/gamut_worker.py" --region $R >/dev/null 2>&1
echo "[$(date '+%F %T')] s3 push ok" >> ~/s3_push.log
