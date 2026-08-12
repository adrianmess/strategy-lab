#!/bin/bash
# Gamut worker control — runs on macOS and Linux, locally or piped over ssh
# (ssh host 'bash -s' status < this file). Signals INDIVIDUAL PIDs of the
# worker tree only (never process groups — nothing outside can be touched).
# Usage: gamut_ctl.sh status|pause|resume
PAT="gamut_worker.py --plan"
tree(){ local p; for p in "$@"; do echo "$p"; tree $(pgrep -P "$p" 2>/dev/null); done; }
# only real python workers — chained shell watchers ("while pgrep …") also
# match the pattern but are not workers
ROOTS=""
for r in $(pgrep -f "$PAT" 2>/dev/null); do
  case "$(ps -o comm= -p "$r" 2>/dev/null)" in
    *[Pp]ython*) ROOTS="$ROOTS $r";;
  esac
done
ROOTS=$(echo $ROOTS)

case "${1:-status}" in
  status)
    if [ -z "$ROOTS" ]; then echo "STATE idle"; exit 0; fi
    ST=running
    for r in $ROOTS; do
      s=$(ps -o stat= -p "$r" 2>/dev/null)
      case "$s" in *T*) ST=paused;; esac
    done
    echo "STATE $ST"
    for r in $ROOTS; do
      ps -o command= -p "$r" 2>/dev/null | grep -o "campaigns/[^/ ]*" | head -1
    done | sort -u | sed 's|campaigns/|PLAN |'
    echo "PIDS $(tree $ROOTS | sort -un | wc -l | tr -d ' ')"
    ;;
  pause)
    [ -z "$ROOTS" ] && { echo "NOTHING"; exit 0; }
    for p in $(tree $ROOTS | sort -un); do kill -STOP "$p" 2>/dev/null; done
    # deadlock guard: a frozen child caught mid-publish holds the
    # backtests.js flock and would block every other publisher on the
    # machine (fcfsx reruns, merges, the panel). Un-freeze lock holders —
    # they finish their seconds-long write, exit, and release the lock.
    if command -v lsof >/dev/null 2>&1; then
      for L in "$HOME/strategy-lab/dashboard/backtests.js.lock" \
               "$HOME/Code/strategy-lab/dashboard/backtests.js.lock"; do
        [ -e "$L" ] || continue
        for p in $(lsof -t "$L" 2>/dev/null); do
          case "$(ps -o stat= -p "$p" 2>/dev/null)" in
            *T*) kill -CONT "$p" 2>/dev/null
                 echo "RELEASED lock-holder $p (finishes its publish)";;
          esac
        done
      done
    fi
    echo "PAUSED"
    ;;
  resume)
    if [ -z "$ROOTS" ]; then echo "NOTHING"; exit 0; fi
    for p in $(tree $ROOTS | sort -un); do kill -CONT "$p" 2>/dev/null; done
    echo "RESUMED"
    ;;
esac
