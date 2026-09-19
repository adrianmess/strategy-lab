#!/bin/bash
# Gamut worker control — runs on macOS and Linux, locally or piped over ssh
# (ssh host 'bash -s' status < this file). Signals INDIVIDUAL PIDs of the
# worker tree only (never process groups — nothing outside can be touched).
# Usage: gamut_ctl.sh status|pause|resume|cores [N]
PAT="gamut_worker.py --plan"
# the repo lives in a different place on each machine
OPTDIR=""
# prefer the dir that actually HOLDS the worker: a phantom ~/strategy-lab
# (created once by a mispathed sync loop) made 'cores' write a limits file
# the real worker never reads (bit the MacBook 2026-09-15)
for d in "$HOME/strategy-lab/optimizer" "$HOME/Code/strategy-lab/optimizer"; do
  [ -f "$d/gamut_worker.py" ] && OPTDIR="$d" && break
done
[ -z "$OPTDIR" ] && for d in "$HOME/strategy-lab/optimizer" "$HOME/Code/strategy-lab/optimizer"; do
  [ -d "$d" ] && OPTDIR="$d" && break
done
LIMITS="$OPTDIR/gamut_limits.json"
# Descend the process tree from ONE ps snapshot. The old version recursed
# with a `pgrep -P` per node: on a 192-vCPU box running 17 searches that is
# 250+ forks, each scanning a huge process table under load ~160, so `status`
# took ~27s — past the panel's 20s probe timeout, which made both healthy EC2
# boxes show "unreachable" on the progress page (2026-09-19). Now one fork.
tree(){
  [ $# -gt 0 ] || return 0
  ps -Ao pid=,ppid= 2>/dev/null | awk -v roots="$*" '
    { kid[$2] = kid[$2] " " $1 }
    END {
      n = split(roots, q, " ")
      for (i = 1; i <= n; i++)
        if (q[i] != "") { out[q[i]] = 1; stack[++top] = q[i] }
      while (top > 0) {
        p = stack[top--]
        m = split(kid[p], c, " ")
        for (j = 1; j <= m; j++)
          if (c[j] != "" && !(c[j] in out)) { out[c[j]] = 1; stack[++top] = c[j] }
      }
      for (p in out) print p
    }'
}
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
  cores)
    if [ -n "$2" ]; then
      [ -n "$OPTDIR" ] || { echo "ERROR no optimizer dir"; exit 1; }
      printf '{"cores": %d}\n' "$2" > "$LIMITS"
      echo "CORES $2 (applies as running searches finish)"
      # EC2 boxes run ec2_size_cores.sh from cron to stop the repo bundle's
      # limits file re-capping them. It is raise-only, so it would undo a
      # deliberate throttle to under half the box — record the intent here so
      # it doesn't, and clear it again when the budget is put back up.
      N=$( (command -v nproc >/dev/null 2>&1 && nproc) \
           || sysctl -n hw.ncpu 2>/dev/null || echo 0 )
      if [ "$N" -gt 0 ] 2>/dev/null && [ "$2" -lt $(( N / 2 )) ] 2>/dev/null; then
        touch "$HOME/NO_AUTOSIZE"
        echo "AUTOSIZE off (~/NO_AUTOSIZE) — $2 is under half of $N cores;"
        echo "  raise it back to >= $(( N / 2 )) to re-enable auto-sizing"
      elif [ -e "$HOME/NO_AUTOSIZE" ]; then
        rm -f "$HOME/NO_AUTOSIZE"
        echo "AUTOSIZE on (~/NO_AUTOSIZE cleared)"
      fi
    else
      echo "CORES $(sed -n 's/.*"cores"[^0-9]*\([0-9]*\).*/\1/p' "$LIMITS" 2>/dev/null)"
    fi
    exit 0
    ;;
  status)
    NPROC=$( (command -v nproc >/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo "?")
    echo "NPROC $NPROC"
    echo "CORES $(sed -n 's/.*"cores"[^0-9]*\([0-9]*\).*/\1/p' "$LIMITS" 2>/dev/null)"
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
