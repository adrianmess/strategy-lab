#!/bin/bash
# MacBook gamut agent — makes "Run on: MacBook" in the panel's Gamut card
# work. Installed as a LaunchAgent (com.strategylab.gamut-agent, every 60s).
#
# Each tick:
#   1. asks the mini (ssh + loopback, so no panel key needed) for campaigns
#      assigned to the MacBook that still have pending specs
#   2. for each: syncs the campaign dir, the pairs' candle data (clearing the
#      local wf2 cache for any pair whose data changed), and done-markers
#   3. starts gamut_worker (cores from optimizer/gamut_limits.json, default
#      12) under caffeinate + a mac2_sync loop, if not already running
#   4. when nothing is pending and a worker/sync finished, does a final sync
# Logs: ~/Code/strategy-lab/optimizer/harvx/../gamut_agent.log (repo/optimizer)
set -u
L="$HOME/Code/strategy-lab"
MINI="admn@admns-Mac-mini.local"
# launchd's PATH only has the system python (no numpy) — pin the framework one
PY="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
SSHOPTS="-i $HOME/.ssh/lab_auto_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8"
LOG="$L/optimizer/gamut_agent.log"
LOCK="/tmp/strategylab_gamut_agent.lock"

exec 9>"$LOCK"
if ! /usr/bin/env flock -n 9 2>/dev/null; then
  # macOS has no flock binary by default — fall back to a pgrep guard
  n=$(pgrep -f "macbook_gamut_agent.sh" | wc -l | tr -d " ")
  [ "$n" -gt 2 ] && exit 0
fi

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

Q=$(ssh $SSHOPTS "$MINI" "curl -s -m 20 http://127.0.0.1:8800/api/gamut/remote_queue" 2>/dev/null) || exit 0
echo "$Q" | grep -q '"queue"' || exit 0
cleanup(){
  # stop sync loops whose campaign is no longer queued (runs even when the
  # queue is EMPTY — the old flow exited first and loops ran forever,
  # rsyncing 30k run dirs through the router every 5 minutes)
  for pid in $(pgrep -f "mac2_sync.sh" 2>/dev/null); do
    camp=$(ps -o command= -p "$pid" | awk "{print \$NF}")
    echo "$NAMES" | grep -q "gamut_${camp}" && continue
    if ! pgrep -f "gamut_worker.py --plan.*gamut_${camp}" >/dev/null; then
      bash "$L/scripts/mac2_sync.sh" "$camp" --once >> "$LOG" 2>&1
      kill "$pid" 2>/dev/null
      log "$camp: complete — final sync done, sync loop stopped"
    fi
  done
}
NAMES=$(echo "$Q" | /usr/bin/python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for c in d.get('queue',[]): print(c['dir'], ','.join(c.get('pairs') or []))")
[ -z "$NAMES" ] && { cleanup; exit 0; }
cleanup

CORES=$(/usr/bin/python3 -c "
import json
try: print(json.load(open('$L/optimizer/gamut_limits.json')).get('cores',12))
except Exception: print(12)")

while read -r DIRN PAIRS; do
  [ -z "$DIRN" ] && continue
  CAMP="${DIRN#gamut_}"
  # 1) campaign dir
  rsync -a --exclude logs -e "ssh $SSHOPTS" \
    "$MINI:strategy-lab/optimizer/campaigns/$DIRN" "$L/optimizer/campaigns/" 2>>"$LOG"
  # 2) candle data for its pairs; clear wf2 cache when a file changed
  for p in $(echo "$PAIRS" | tr "," " "); do
    CH=$(rsync -ai -e "ssh $SSHOPTS" \
      "$MINI:strategy-lab/adaptive_trader/research/data/${p}_*min.parquet" \
      "$L/adaptive_trader/research/data/" 2>>"$LOG" | grep -c "^>f") || true
    if [ "${CH:-0}" -gt 0 ]; then
      rm -rf "$L/optimizer/cache/$p"
      log "$CAMP: refreshed $p data ($CH file(s)) — cleared its wf2 cache"
    fi
  done
  # 3) done markers from the mini
  rsync -a --ignore-existing \
    --include="/${CAMP}_*/" --include="/${CAMP}_*/best_config.json" \
    --include="/${CAMP}_*/no_survivor.json" --exclude="*" \
    -e "ssh $SSHOPTS" "$MINI:strategy-lab/optimizer/runs/" \
    "$L/optimizer/runs/" 2>>"$LOG"
  # 4) worker + sync loop (one each per campaign)
  if ! pgrep -f "gamut_worker.py --plan.*$DIRN" >/dev/null; then
    log "$CAMP: starting gamut_worker (cores=$CORES)"
    (cd "$L/optimizer" && nohup caffeinate -i "$PY" \
      gamut_worker.py --plan "campaigns/$DIRN/plan.json" --cores "$CORES" \
      >> "campaigns/$DIRN/worker_macbook.log" 2>&1 &)
  fi
  if ! pgrep -f "mac2_sync.sh $CAMP" >/dev/null; then
    log "$CAMP: starting mac2_sync loop"
    (nohup bash "$L/scripts/mac2_sync.sh" "$CAMP" \
      >> "$L/optimizer/campaigns/$DIRN/sync_macbook.log" 2>&1 &)
  fi
done <<< "$NAMES"

exit 0
