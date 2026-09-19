#!/bin/bash
# Size this machine's gamut core budget from the cores it ACTUALLY has.
#
# WHY THIS EXISTS
# ---------------
# optimizer/gamut_limits.json is machine-local but lives inside the repo, so
# it rides along in repo.tgz / rsync / scp of the tree. On 2026-09-19 the Mac
# mini's {"cores": 10} reached two 192-vCPU EC2 boxes that way. gamut_worker's
# budget() treats that file as AUTHORITATIVE over --jobs, so both boxes ran
# ONE search at 10 procs: load ~6, ~95% idle, at full spot price, for hours.
# The only symptom was a single line in worker.log.
#
# Every EC2 boot script calls this before starting a worker, and cron re-runs
# it every 10 minutes so a later repo re-extract can't silently re-cap the box.
#
# SIZING: jobs = nproc / 11 (min 4), cores = jobs x procs-per-spec (from the
# plan, default 14). Oversubscription is measured-optimal — see the runbook.
#
# SAFETY — it will never slow a machine down:
#   * raise-only: a budget at or above nproc/2 is left exactly as it is
#   * ~/NO_AUTOSIZE present  -> does nothing at all, ever (deliberate throttle)
#   * `gamut_ctl.sh cores N` with a small N creates NO_AUTOSIZE for you
#   * changes are picked up LIVE by the worker — this never restarts anything
#
# Usage: ec2_size_cores.sh [--quiet] [--force] [--dry-run]
set -u
QUIET=0; FORCE=0; DRY=0
for a in "$@"; do
  case "$a" in
    --quiet) QUIET=1;; --force) FORCE=1;; --dry-run) DRY=1;;
    -h|--help) sed -n '2,28p' "$0"; exit 0;;
  esac
done
say(){ [ "$QUIET" = 1 ] || echo "$@"; }
log(){ echo "[$(date '+%F %T')] size_cores: $*" >> "$HOME/boot_workers.log" 2>/dev/null; }

# --- locate the optimizer dir that actually holds the worker ----------------
# (same rule as gamut_ctl.sh: a phantom ~/strategy-lab once made a sibling
# script write a limits file the real worker never read)
OPTDIR=""
for d in "$HOME/strategy-lab/optimizer" "$HOME/Code/strategy-lab/optimizer"; do
  [ -f "$d/gamut_worker.py" ] && OPTDIR="$d" && break
done
[ -n "$OPTDIR" ] || { say "size_cores: no optimizer dir yet — nothing to do"; exit 0; }
LIMITS="$OPTDIR/gamut_limits.json"

if [ -e "$HOME/NO_AUTOSIZE" ] && [ "$FORCE" != 1 ]; then
  say "size_cores: ~/NO_AUTOSIZE present — leaving $LIMITS alone"
  exit 0
fi

NPROC=$( (command -v nproc >/dev/null 2>&1 && nproc) \
         || sysctl -n hw.ncpu 2>/dev/null || echo 0 )
[ "$NPROC" -gt 0 ] 2>/dev/null || { say "size_cores: cannot read cpu count"; exit 0; }

# --- procs per search: ask the plan, fall back to the historical default ----
PROCS=14
PLAN=""
[ -f "$HOME/PLAN_NAME" ] && PLAN=$(cat "$HOME/PLAN_NAME" 2>/dev/null)
if [ -n "$PLAN" ] && [ -f "$OPTDIR/campaigns/$PLAN/plan.json" ]; then
  P=$(python3 - "$OPTDIR/campaigns/$PLAN/plan.json" <<'PY' 2>/dev/null
import json, sys
try:
    specs = json.load(open(sys.argv[1]))
    if isinstance(specs, dict):
        specs = specs.get("specs") or specs.get("candidates") or []
    cmd = list((specs[0] or {}).get("cmd") or [])
    print(int(cmd[cmd.index("--procs") + 1]))
except Exception:
    pass
PY
)
  [ -n "${P:-}" ] && [ "$P" -gt 0 ] 2>/dev/null && PROCS=$P
fi

# Floor of 2, not the boot scripts' 4: on the 192-vCPU workhorses nproc/11 is
# 17 so the floor never binds, but on a small box "min 4 x 14 procs" would ask
# for 56 core-equivalents from 8 cores. Cap at 1.5x nproc for the same reason.
JOBS=$(( NPROC / 11 )); [ "$JOBS" -lt 2 ] && JOBS=2
WANT=$(( JOBS * PROCS ))
MAXW=$(( NPROC * 3 / 2 )); [ "$MAXW" -lt "$PROCS" ] && MAXW=$PROCS
[ "$WANT" -gt "$MAXW" ] && WANT=$MAXW

CUR=$(sed -n 's/.*"cores"[^0-9]*\([0-9]*\).*/\1/p' "$LIMITS" 2>/dev/null)
[ -n "${CUR:-}" ] || CUR=0

# raise-only. A budget already at half the box or more is somebody's choice
# (or already ours) — never lower it, and never fight a deliberate throttle.
if [ "$FORCE" != 1 ] && [ "$CUR" -ge $(( NPROC / 2 )) ] 2>/dev/null; then
  say "size_cores: cores=$CUR on ${NPROC}c — already sized, leaving alone"
  exit 0
fi

if [ "$DRY" = 1 ]; then
  echo "size_cores: WOULD set cores=$WANT (was $CUR) on ${NPROC}c" \
       "[$JOBS searches x $PROCS procs]"
  exit 0
fi

TMP="$LIMITS.tmp.$$"
printf '{"cores": %d, "auto": true, "nproc": %d, "jobs": %d, "procs": %d, "set": "%s", "host": "%s"}\n' \
  "$WANT" "$NPROC" "$JOBS" "$PROCS" "$(date '+%F %T')" "$(hostname -s 2>/dev/null)" > "$TMP" \
  && mv -f "$TMP" "$LIMITS"

MSG="cores $CUR -> $WANT on ${NPROC} vCPU ($JOBS searches x $PROCS procs)"
if [ "$CUR" -gt 0 ] && [ "$CUR" -lt $(( NPROC / 2 )) ]; then
  MSG="$MSG  <-- CORRECTED an inherited budget (was using $(( CUR * 100 / NPROC ))% of this box)"
fi
say "size_cores: $MSG"
log "$MSG"
