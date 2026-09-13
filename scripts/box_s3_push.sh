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
  # pull PEER states down so --loop workers can see (and not duplicate)
  # what the other box is actively running near the meet point.
  # PERSISTENT cache dir + cp -p: s3 sync only touches files that actually
  # changed, so a dead peer's state keeps its old mtime and the worker's
  # 20-min staleness check works (a fresh temp dir re-downloaded everything
  # and made every peer look alive forever)
  T="$HOME/.gwstate_cache/$(basename "$c")"
  mkdir -p "$T"
  aws s3 sync "$B/state/$(basename "$c")/" "$T/" --region $R >/dev/null 2>&1
  for f in "$T"/worker_state_*.json; do
    [ -e "$f" ] || continue
    h=$(basename "$f" .json); h=${h#worker_state_}
    [ "$h" = "$(hostname)" ] && continue
    cp -p "$f" "$c/worker_state_peer_$h.json" 2>/dev/null
  done
done
aws s3 cp dashboard/backtests.js "$B/backtests/backtests_$(hostname).js" --region $R >/dev/null 2>&1
aws s3 cp "$HOME/strategy-lab/optimizer/gamut_worker.py" "$B/code/gamut_worker.py" --region $R >/dev/null 2>&1
echo "[$(date '+%F %T')] s3 push ok" >> ~/s3_push.log
