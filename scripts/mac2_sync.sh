#!/bin/bash
# mac2_sync.sh — runs ON THE MACBOOK while it helps the Mac mini with a gamut
# campaign (meet-in-the-middle: mini forward, MacBook --reverse).
#
# Every 5 minutes:
#   1. pull the mini's done-markers (best_config/no_survivor) so the local
#      gamut_worker never repeats a spec the mini finished
#   2. push completed local run dirs to the mini (its worker skips them, its
#      Backtests/Progress pages see them)
#   3. push local worker_state.json as worker_state_b.json (the Progress page
#      merges worker_state*.json)
#   4. push local backtests.js for an additive merge on the mini
#
# Usage: mac2_sync.sh <campaign>           e.g. mac2_sync.sh g0824_0103
# Stop:  pkill -f mac2_sync.sh
CAMP="${1:?campaign name, e.g. g0824_0103}"
MINI="admn@admns-Mac-mini.local"
SSHOPTS="-i $HOME/.ssh/lab_auto_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
L="$HOME/Code/strategy-lab"
R="strategy-lab"

sync_once() {
  # 1) done-markers from the mini (filter rules, not a remote glob — the
  #    expanded arg list once blew the fd limit at ~280 dirs)
  rsync -a --bwlimit=1500 --ignore-existing \
    --include="/${CAMP}_*/" \
    --include="/${CAMP}_*/best_config.json" \
    --include="/${CAMP}_*/no_survivor.json" \
    --include="/${CAMP}_*/holdout_best_config.json" \
    --exclude="*" \
    -e "ssh $SSHOPTS" "$MINI:$R/optimizer/runs/" "$L/optimizer/runs/"
  # 2) our completed runs -> mini (only dirs that hold a durable marker)
  for d in "$L"/optimizer/runs/${CAMP}_*/; do
    [ -e "$d/best_config.json" ] || [ -e "$d/no_survivor.json" ] || continue
    rsync -a --bwlimit=1500 --ignore-existing -e "ssh $SSHOPTS" "$d" \
      "$MINI:$R/optimizer/runs/$(basename "$d")/"
  done
  # 3) worker state for the Progress page
  if [ -f "$L/optimizer/campaigns/gamut_$CAMP/worker_state.json" ]; then
    rsync -a --bwlimit=1500 -e "ssh $SSHOPTS" \
      "$L/optimizer/campaigns/gamut_$CAMP/worker_state.json" \
      "$MINI:$R/optimizer/campaigns/gamut_$CAMP/worker_state_b.json"
  fi
  # 4) backtests merge (additive by name, flock-safe on the mini side)
  if [ -f "$L/dashboard/backtests.js" ]; then
    rsync -a --bwlimit=1500 -e "ssh $SSHOPTS" "$L/dashboard/backtests.js" \
      "$MINI:/tmp/backtests_macbook.js"
    ssh $SSHOPTS "$MINI" \
      "cd $R && ~/venv/bin/python3 scripts/merge_backtests.py /tmp/backtests_macbook.js" \
      >/dev/null 2>&1
  fi
}

if [ "$2" = "--once" ]; then sync_once; exit 0; fi
while :; do
  sync_once
  sleep 300
done
