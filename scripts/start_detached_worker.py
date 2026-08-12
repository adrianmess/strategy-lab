#!/usr/bin/env python3
"""Start a gamut worker fully detached: double-fork + setsid so it owns its
process session/group — pausing its tree can never touch the launcher, and
app restarts can't reap it.
Usage: python3 start_detached_worker.py <plan> <jobs> <procs_cap> <logfile> [--reverse]
"""
import os, sys

plan, jobs, cap, log = sys.argv[1:5]
rev = "--reverse" in sys.argv[5:]
OPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "optimizer")

if os.fork():
    os._exit(0)          # parent returns immediately
os.setsid()              # new session: own pgid, no controlling terminal
if os.fork():
    os._exit(0)          # first child exits; grandchild is fully detached
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.close(0)
os.chdir(OPT)
cmd = ["caffeinate", "-i", sys.executable, "gamut_worker.py",
       "--plan", plan, "--jobs", jobs, "--procs-cap", cap]
if rev:
    cmd.append("--reverse")
os.execvp("caffeinate", cmd)
