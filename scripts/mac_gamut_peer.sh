#!/bin/bash
# Make a Mac a first-class peer in a gamut campaign alongside the EC2 boxes.
# Cron every 5 min. The Mac equivalent of box_s3_push.sh, with three
# differences that matter:
#
#  1. No `aws` binary on the MacBook — the module is there, so `python3 -m
#     awscli` is used throughout (credentials come from ~/.aws as usual).
#  2. box_s3_push does `aws s3 sync optimizer/runs` wholesale. That is safe on
#     a box whose runs/ holds only this campaign; on the MacBook runs/ is
#     ~27k dirs and 53GB, so EVERY transfer here is filtered to the campaign
#     prefix. Do not relax that.
#  3. A box's results reach the hub because the mini rsyncs them down. Nothing
#     pulls from the MacBook, so it pushes its own finished runs to the mini.
#
# WHY THE STATE PUSH IS THE IMPORTANT PART: running workers never re-read each
# other's run dirs — done-markers are only consulted at startup. Mid-campaign,
# the ONLY thing stopping two machines from searching the same spec is
# worker_state_peer_*.json (see _peer_running() in gamut_worker.py, 20-min
# staleness window). If this script stops, the Mac silently starts duplicating
# the boxes' work.
#
# Usage: mac_gamut_peer.sh [campaign]   (default: gamut_hfee_mh12)
set -u
CAMP="${1:-gamut_hfee_mh12}"
B="s3://gamut-sync-637309463295"
R="us-east-2"
REPO="$HOME/Code/strategy-lab"
MINI="admn@admns-Mac-mini.local"
KEY="$HOME/.ssh/lab_auto_ed25519"
LOG="$HOME/mac_gamut_peer.log"
H=$(hostname -s | tr -cd 'A-Za-z0-9-')
AWS="python3 -m awscli"
CDIR="$REPO/optimizer/campaigns/$CAMP"
say(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

[ -d "$CDIR" ] || { say "no campaign $CAMP — nothing to do"; exit 0; }
cd "$REPO" || exit 0

# ---- 1. publish OUR claims so the boxes skip what we are running -----------
if [ -f "$CDIR/worker_state.json" ]; then
  $AWS s3 cp "$CDIR/worker_state.json" \
      "$B/state/$CAMP/worker_state_$H.json" --region $R --only-show-errors \
      >/dev/null 2>&1 || say "WARN state push failed"
fi

# ---- 2. pull the peers' claims so WE skip what they are running ------------
# Persistent cache + cp -p, same as box_s3_push: a fresh temp dir would
# re-download everything and reset mtimes, making a dead peer look alive
# forever and freezing this worker out of the specs it abandoned.
T="$HOME/.gwstate_cache/$CAMP"
mkdir -p "$T"
$AWS s3 sync "$B/state/$CAMP/" "$T/" --region $R --only-show-errors >/dev/null 2>&1
for f in "$T"/worker_state_*.json; do
  [ -e "$f" ] || continue
  h=$(basename "$f" .json); h=${h#worker_state_}
  [ "$h" = "$H" ] && continue
  cp -p "$f" "$CDIR/worker_state_peer_$h.json" 2>/dev/null
done

# ---- 3. deliver OUR finished runs to the hub (and to S3) -------------------
# Campaign prefix only — see note 2 in the header.
PRE="${CAMP#gamut_}"          # gamut_hfee_mh12 -> hfee_mh12
if ls -d optimizer/runs/${PRE}_* >/dev/null 2>&1; then
  rsync -a --timeout=120 \
    -e "ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10" \
    --include="${PRE}_*/" --include="${PRE}_*/**" --exclude="*" \
    optimizer/runs/ "$MINI:strategy-lab/optimizer/runs/" >/dev/null 2>&1 \
    || say "WARN rsync to mini failed"
  $AWS s3 sync optimizer/runs "$B/runs" --region $R --size-only \
      --exclude "*" --include "${PRE}_*/*" --exclude "*_backtest_tmp*" \
      --only-show-errors >/dev/null 2>&1 || say "WARN s3 runs push failed"
fi

N=$(ls -d optimizer/runs/${PRE}_* 2>/dev/null | wc -l | tr -d ' ')
P=$(ls "$CDIR"/worker_state_peer_*.json 2>/dev/null | wc -l | tr -d ' ')
say "peer sync ok — $N ${PRE} dirs local, $P peer state file(s)"
