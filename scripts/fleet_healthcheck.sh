#!/bin/bash
# Watch the gamut EC2 fleet FROM THE MINI and repair the failures that a box
# cannot notice about itself. Cron every 15 min. Safe to run by hand.
#
# WHY IT EXISTS
# -------------
# gamut_limits.json is machine-local but ships inside the repo bundle, and
# gamut_worker's budget() treats it as authoritative over --jobs. Twice now a
# box inherited the mini's {"cores": 10} and quietly ran ONE search on 192
# vCPUs — ~95% idle at full spot price, for 17 hours the second time
# (2026-09-19 and again 09-20). ec2_size_cores.sh on each box fixes this in
# 10-minute cron, but the 09-20 recurrence happened precisely BECAUSE that
# script and its cron were missing: a spot replacement booted from the launch
# template's stale embedded user-data and never installed them. A box cannot
# self-heal with the self-healer absent, so the check has to come from here.
#
# WHAT IT REPAIRS (only the provably safe, well-understood ones):
#   * an undersized core budget      -> re-size from nproc
#   * ec2_size_cores.sh missing      -> fetch from S3
#   * its self-heal cron missing     -> install
# WHAT IT ONLY REPORTS (needs a human decision):
#   * box unreachable, worker dead, fee guard tripped, no campaign
# It NEVER touches traders, never restarts a running worker, and never lowers
# a budget (ec2_size_cores.sh is raise-only and honours ~/NO_AUTOSIZE).
#
# Usage: fleet_healthcheck.sh [--verbose]
set -u
export PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:$PATH
KEY="$HOME/.ssh/gamut-key.pem"
BUCKET="s3://gamut-sync-637309463295"
LOG="$HOME/fleet_health.log"
STATE="$HOME/.fleet_health_state"
VERBOSE=0
[ "${1:-}" = "--verbose" ] && VERBOSE=1
say(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; [ "$VERBOSE" = 1 ] && echo "$*"; }
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
 -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 \
 -o BatchMode=yes"

# ---- which boxes? discovered from the running sync loop, so a spot
# ---- replacement's new IP is picked up without editing this file ----------
HOSTS=$(ps -Ao command | sed -n 's/.*offload_sync\.sh [^ ]* [^ ]* //p' \
        | tr ' ' '\n' | sed 's/:.*//' | grep -E '^[0-9.]+$' | sort -u)
if [ -z "$HOSTS" ]; then
  say "no offload_sync loop running — no fleet to check (campaign finished, or sync is down)"
  exit 0
fi
[ -f "$KEY" ] || { say "ERROR ssh key $KEY missing — cannot check the fleet"; exit 1; }

FIXED=0; PROBLEMS=""
for H in $HOSTS; do
  # One round trip collects everything; keep it cheap, this runs every 15 min.
  R=$($SSH "ubuntu@$H" '
        echo "NPROC=$(nproc)"
        echo "CORES=$(sed -n "s/.*\"cores\"[^0-9]*\([0-9]*\).*/\1/p" \
              ~/strategy-lab/optimizer/gamut_limits.json 2>/dev/null)"
        echo "SIZER=$([ -x ~/ec2_size_cores.sh ] && echo yes || echo no)"
        echo "SCRON=$(crontab -l 2>/dev/null | grep -c ec2_size_cores)"
        echo "LEGS=$(pgrep -fc optimize2_cli || echo 0)"
        echo "WORKER=$(pgrep -fc "gamut_worker.py --plan" || echo 0)"
        echo "NOAUTO=$([ -e ~/NO_AUTOSIZE ] && echo yes || echo no)"
        echo "LOAD=$(cut -d" " -f1 /proc/loadavg)"
        echo "UPS=$(cut -d" " -f1 /proc/uptime | cut -d. -f1)"
      ' 2>/dev/null)

  if [ -z "$R" ]; then
    PROBLEMS="$PROBLEMS\n  $H UNREACHABLE (ssh failed — spot interruption, or still booting)"
    say "$H unreachable"
    continue
  fi
  eval "$(echo "$R" | grep -E '^(NPROC|CORES|SIZER|SCRON|LEGS|WORKER|NOAUTO|LOAD|UPS)=')"
  CORES=${CORES:-0}; NPROC=${NPROC:-0}; UPS=${UPS:-99999}

  # ---- repair 1: the sizer itself went missing (the 2026-09-20 cause) ----
  if [ "$SIZER" = "no" ]; then
    $SSH "ubuntu@$H" "aws s3 cp $BUCKET/code/ec2_size_cores.sh \
         ~/ec2_size_cores.sh --only-show-errors && chmod +x ~/ec2_size_cores.sh" \
         >/dev/null 2>&1 \
      && { say "$H FIXED: ec2_size_cores.sh was missing — reinstalled from S3"; FIXED=1; } \
      || PROBLEMS="$PROBLEMS\n  $H could not install ec2_size_cores.sh from S3"
  fi

  # ---- repair 2: its own self-heal cron went missing ---------------------
  if [ "${SCRON:-0}" = "0" ]; then
    $SSH "ubuntu@$H" '( crontab -l 2>/dev/null | grep -v ec2_size_cores
                        echo "*/10 * * * * ~/ec2_size_cores.sh --quiet" ) | crontab -' \
         >/dev/null 2>&1 \
      && { say "$H FIXED: self-heal cron was missing — installed"; FIXED=1; } \
      || PROBLEMS="$PROBLEMS\n  $H could not install the self-heal cron"
  fi

  # ---- repair 3: the budget itself. Deliberate throttles are respected ---
  if [ "$NOAUTO" = "yes" ]; then
    say "$H cores=$CORES of $NPROC — NO_AUTOSIZE set, leaving alone"
  elif [ "$NPROC" -gt 0 ] && [ "$CORES" -lt $(( NPROC / 2 )) ]; then
    PCT=$(( CORES * 100 / NPROC ))
    OUT=$($SSH "ubuntu@$H" '~/ec2_size_cores.sh' 2>&1)
    NEW=$($SSH "ubuntu@$H" 'sed -n "s/.*\"cores\"[^0-9]*\([0-9]*\).*/\1/p" \
          ~/strategy-lab/optimizer/gamut_limits.json' 2>/dev/null)
    if [ "${NEW:-0}" -gt "$CORES" ] 2>/dev/null; then
      say "$H FIXED: core budget was $CORES on ${NPROC} vCPU (${PCT}% of the box) -> $NEW"
      FIXED=1
      CORES=$NEW      # so the summary line below reports the REPAIRED value —
                      # the Claude review task reads these lines, and a stale
                      # "cores=10/192" after a successful fix reads as a failure
    else
      PROBLEMS="$PROBLEMS\n  $H core budget stuck at $CORES of $NPROC vCPU — re-size failed: $OUT"
    fi
  fi

  # ---- report-only checks ------------------------------------------------
  [ "${WORKER:-0}" = "0" ] && \
    PROBLEMS="$PROBLEMS\n  $H gamut worker NOT RUNNING (box is up; needs a look)"
  # a box under 10 min old is still ramping (17 searches start 20s apart, so
  # ~6 min to full) — 2026-09-21 03:15 flagged a 90-second-old replacement
  # with legs=4 as a problem; it was at 256 by the next check
  if [ "${WORKER:-0}" != "0" ] && [ "${LEGS:-0}" -lt 20 ] && [ "$NPROC" -gt 64 ] \
     && [ "$UPS" -ge 600 ]; then
    PROBLEMS="$PROBLEMS\n  $H only ${LEGS} search processes on ${NPROC} vCPU (load ${LOAD:-?}) — something is capping it (box is $(( UPS / 60 )) min old, past ramp-up)"
  fi
  say "$H ok — cores=$CORES/$NPROC legs=${LEGS:-?} load=${LOAD:-?} worker=${WORKER:-0}"
done

if [ -n "$PROBLEMS" ]; then
  printf "[%s] PROBLEMS NEEDING A HUMAN:%b\n" "$(date '+%F %T')" "$PROBLEMS" >> "$LOG"
  [ "$VERBOSE" = 1 ] && printf "PROBLEMS:%b\n" "$PROBLEMS"
fi
echo "$(date '+%F %T') fixed=$FIXED problems=$([ -n "$PROBLEMS" ] && echo yes || echo no)" \
  > "$STATE"
exit 0
