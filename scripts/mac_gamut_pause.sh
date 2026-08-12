#!/bin/bash
# Pause/resume THIS Mac's gamut worker without losing work.
# v2: signals INDIVIDUAL PIDs of the worker's process tree (gamut_worker +
# its optimize2 searches + their pool children). Never touches process
# groups, so nothing outside the worker tree can be affected.
# Usage: mac_gamut_pause.sh pause|resume|status [plan-substring]
ACT="${1:-status}"
PAT="${2:-gamut_worker.py --plan}"
PIDFILE=/tmp/gamut_paused_pids

tree(){ local p; for p in "$@"; do echo "$p"; tree $(pgrep -P "$p") ; done; }

case "$ACT" in
  pause)
    ROOTS=$(pgrep -f "$PAT")
    [ -z "$ROOTS" ] && { echo "no gamut worker matching '$PAT'"; exit 0; }
    PIDS=$(tree $ROOTS | sort -un)
    for p in $PIDS; do kill -STOP "$p" 2>/dev/null; done
    echo "$PIDS" > "$PIDFILE"
    echo "PAUSED $(echo "$PIDS" | wc -w | tr -d ' ') processes (worker tree only)."
    echo "resume with: $0 resume";;
  resume)
    if [ -s "$PIDFILE" ]; then
      for p in $(cat "$PIDFILE"); do kill -CONT "$p" 2>/dev/null; done
      rm -f "$PIDFILE"; echo "RESUMED from $PIDFILE"
    else
      ROOTS=$(pgrep -f "$PAT"); [ -z "$ROOTS" ] && { echo "nothing to resume"; exit 0; }
      for p in $(tree $ROOTS | sort -un); do kill -CONT "$p" 2>/dev/null; done
      echo "RESUMED (tree walk)"
    fi;;
  status)
    ROOTS=$(pgrep -f "$PAT")
    [ -z "$ROOTS" ] && { echo "no gamut worker running"; exit 0; }
    ps -o pid,stat,%cpu,etime,comm -p $(tree $ROOTS | sort -un | tr '\n' ' ') 2>/dev/null;;
esac
