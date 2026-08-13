# EC2 Gamut Offload — Runbook

How to rebuild the AWS compute rig that ran the g0801_2122 (12,960 specs) and
ghype (2,592 specs) gamut campaigns in August 2026. Everything referenced here
lives in this repo; nothing depends on the torn-down AWS resources.

**The one-line summary:** big spot instances run `gamut_worker.py` against a
campaign plan, durable results land in `optimizer/runs/<name>/` as
`best_config.json` or `no_survivor.json`, an S3 bucket is the rendezvous
between boxes and the Mac pulls everything home every 5 minutes.

---

## 1. What you need before starting

- **AWS account** (was 637309463295, us-east-2). Sign-in is always yours —
  never share credentials with the agent; it drives the console via the
  browser or CloudShell while you're signed in.
- **Spot vCPU quota** (`L-34B43A08`, "All Standard Spot Instance Requests").
  Fresh accounts get 32 — request 300+ in Service Quotas *first*; approval
  took hours-to-a-day and one follow-up case. 192 vCPU (c7a.48xlarge) ≈ one
  box A; 96 more for a second box.
- **Key pair**: `gamut-key` (`~/Downloads/gamut-key.pem` on the Mac).
- **Security group** `gamut-ssh`: SSH (22) inbound from your home IP only.
- **Two Elastic IPs** (box A, box B) so replacements keep stable addresses.
  NOTE: host keys rotate on every replacement — all scripts use
  `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`.

### Instance choice (measured)
- **c7a.48xlarge** (192 vCPU) spot ≈ $2.9-3.5/hr in us-east-2 — the workhorse.
- c8a.48xlarge slightly faster when available; c6a.48xlarge the fallback.
- Worker sizing rule: **jobs = nproc / 11** (each spec runs 14 procs;
  oversubscription measured optimal), min 4.
- Full campaign cost: ~$300 total for ~15,500 specs over ~1 week with two
  boxes, vs an estimated 20+ days locally.

---

## 2. Bootstrap a box from a plain Ubuntu 22.04 AMI

`scripts/ec2_bootstrap.sh` has the original steps. Summary:

```
sudo apt update && sudo apt install -y python3-venv tmux
python3 -m venv ~/venv
. ~/venv/bin/activate
pip install numpy pandas numba pyarrow requests python-dotenv
#   exact known-good versions: docs/ec2/box_snapshot.txt (numpy 2.4.6,
#   pandas 3.0.5, numba 0.66.0, pyarrow 24.0.0, llvmlite 0.48.0)
git clone <this repo> ~/strategy-lab      # or rsync from the Mac
#   plus the candle data: rsync data/ (~590MB) — caches rebuild on demand,
#   do NOT ship the 36GB cache directory
sudo loginctl enable-linger ubuntu        # CRITICAL — see Lessons #1
sudo timedatectl set-timezone America/Los_Angeles   # see Lessons #6
```

Then bake an **AMI** of the finished box. A fresh AMI-launched instance is
worker-ready in ~15 min in any AZ/type — this was the single most valuable
resilience move.

## 3. Per-box files (all in `scripts/`, shipped to `~` on the box)

| file | role |
|---|---|
| `ec2_boot_workers.sh` | box A boot: `keeper` + `gamut` (main plan, forward) + `hype` tmux sessions, session-existence guards, `J=$(nproc/11)` |
| `ec2_boot_workers_b.sh` | box B boot: same but `--reverse` (meet-in-the-middle), no separate roles |
| `box_s3_push.sh` | cron `*/5`: push runs / worker_state / backtests.js / worker code to S3 |
| `fleet_userdata_a.sh` | launch-template user-data: tag, grab Elastic IP, linger, pull code+markers from S3, install cron, start workers — makes fleet replacements fully self-arming |

Install: `crontab` gets `@reboot sleep 30 && ~/ec2_boot_workers.sh` and
`*/5 * * * * ~/box_s3_push.sh` (see `docs/ec2/box_snapshot.txt`).

The worker itself is `optimizer/gamut_worker.py`:
- claims specs from the campaign plan, **skip-if-done at pickup** (checks
  `runs/<name>/best_config.json` OR `no_survivor.json` — the durable markers)
- writes `no_survivor.json` when a search completes with no feasible config
  (without this, done-counts collapse on every replacement box)
- startup sweep marks stale `running` → `interrupted`; retries carry `try=N`
- `--reverse` walks the plan backward for the second box
- graceful stop: `touch campaigns/<camp>/STOP_WORKER`

## 4. S3 rendezvous + IAM

- Bucket `gamut-sync-<acct>` with prefixes `runs/` `state/` `backtests/` `code/`.
- IAM role **gamut-box** (EC2 instance profile, S3 read/write on that bucket)
  — **no keys ever stored on boxes or in the repo**.
- Boxes push every 5 min (`box_s3_push.sh`); fresh fleet instances pull code
  + done-markers from S3 before starting (user-data), so completions from a
  dead box's lifetime are never redone.

## 5. Spot resilience: two patterns that worked

**Box B (simple):** persistent spot request, stop-on-interrupt, 200GB gp3.
Interruption → instance stops → relaunches when capacity returns → cron
`@reboot` re-arms everything. Cancel the REQUEST before terminating the
instance or it relaunches forever.

**Box A (better): EC2 Fleet** `type=maintain`, capacity-optimized allocation,
9 pools (c8a/c7a/c6a.48xlarge × us-east-2a/b/c), target 1. The fleet hops AZ
and instance type automatically when a pool dries up; the launch-template
user-data (`fleet_userdata_a.sh`) makes each replacement self-arming. Needs
service-linked role `AWSServiceRoleForEC2Fleet`.
us-east-2c was interruption hell (6+/day at times); 2a/2b much calmer.

## 6. Mac side

| file | role |
|---|---|
| `scripts/offload_sync.sh` | 5-min loop: pull completed runs (`--ignore-existing`), push done-markers both ways, pull worker_state per campaign, pull each box's backtests.js → `merge_backtests.py` |
| `scripts/offload_watch.sh` | waits for a box's EIP:22 to answer, ships the boot script, installs cron, restarts the sync loop. Usage: `offload_watch.sh <IP> <PEM> [BOOT_SCRIPT] [SYNC_IP...]` |
| `scripts/merge_backtests.py` | additive-by-name merge into dashboard/backtests.js (prune + slim + opt-stamp + tombstone-aware) |

Dashboard: the **Progress page** reads merged `worker_state*.json` (campaign
dropdown, ETA, retry tags, "no survivor" flags, cost tile); ghost-buster
marks >45-min "running" as interrupted.

**Hourly monitor**: a scheduled task ("ec2-gamut-monitor") checked boxes every
hour and auto-fixed known failure modes (dead sessions → re-arm via boot
script; spot interruption → wait/relaunch per pattern; direction check so
both boxes never run the same order; ≤1 AZ move per box per day; never
touches trading). Recreate it only when a campaign is actually running.

## 7. Launch sequence (condensed)

1. Raise spot quota; create key pair, SG, 2 EIPs, bucket, IAM role gamut-box.
2. Launch one spot box from the AMI (or bootstrap from Ubuntu + bake AMI).
3. Associate EIP; `offload_watch.sh <EIP-A> ~/Downloads/gamut-key.pem ec2_boot_workers.sh <EIP-A> <EIP-B>` from the Mac.
4. Build the campaign plan locally (Gamut card on the Optimize page), rsync
   `optimizer/campaigns/<camp>/` to the box(es).
5. Box B: second spot request (persistent/stop-on-interrupt) or add to fleet;
   boot with `ec2_boot_workers_b.sh` (`--reverse`).
6. Optionally convert box A to an EC2 Fleet (launch template with
   `fleet_userdata_a.sh` as user-data, service-linked role, maintain/1).
7. Recreate the hourly monitor task. Watch the Progress page.

## 8. Teardown checklist (order matters)

1. Final pull: run one sync cycle; verify every plan spec has
   `best_config.json`/`no_survivor.json` locally; pull each box's
   backtests.js and merge.
2. Kill Mac loops: `pkill -f offload_sync.sh; pkill -f offload_watch.sh`.
3. **Delete the fleet** (`delete-fleets --terminate-instances`) — else it
   replaces the instance you just killed. Then delete launch template gamut-A.
4. Box B: **cancel the persistent spot request FIRST**, then terminate.
5. Release both Elastic IPs — REQUIRED (since Feb-2024 they bill $0.005/hr even while attached, and forever once unattached). Current: 3.133.195.5, 3.15.93.224.
6. Deregister the AMI, then delete its snapshot.
7. Empty + delete the S3 bucket.
8. Delete IAM instance profile + role gamut-box.
9. Disable/delete the hourly monitor task.
10. EBS volumes delete on termination (were set that way) — verify none linger.

## 9. Lessons learned (paid for in compute)

1. **systemd reaped user-data-launched workers ~20 min after boot** — always
   `loginctl enable-linger ubuntu`.
2. **Durable evidence only** — state files get clobbered by AMI-fresh boxes.
   Anything that matters must be a file in `runs/<name>/` (this is why
   `no_survivor.json` exists; its absence twice caused done-% to collapse).
3. **tmux**: commands run under dash (`.` not `source`); killing what you
   think is one pane can be the server (all sessions die) — keep a `keeper`
   session; `pkill -f` can match its own ssh shell (bracket trick `[o]pt...`).
4. **Host keys rotate** on every spot replacement — never pin them, or sync
   silently dies for hours.
5. **Concurrent writers corrupt backtests.js** — every publisher must use
   flock + atomic tmp-rename + tolerant raw_decode load (13 writers once
   corrupted it silently).
6. **Set box timezone to America/Los_Angeles** — UTC stamps skewed entry
   ordering by 7h and "hid" new results.
7. **CloudShell drops long commands** (~500 chars max per paste) and its
   get_page_text goes stale — screenshots are ground truth. Console sessions
   hard-expire (~12h): re-sign-in is a human job.
8. **Meet-in-the-middle** (`--reverse` on box 2) needs zero coordination —
   markers + skip-if-done handle collisions for free.
9. Spot in us-east-2c interrupts far more than 2a/2b; capacity-optimized
   fleet across 9 pools basically ended the babysitting.
10. Estimate honestly: ~200-350 specs/hr with both boxes up; per-spec runtime
    varies 4x by strategy/timeframe (1m data is the slow end).

## 10. What was archived where

- All box/Mac scripts: `scripts/` (this repo, git).
- Worker: `optimizer/gamut_worker.py`.
- Exact box environment: `docs/ec2/box_snapshot.txt` (pip freeze, crontab).
- Campaign plans + all 15,552 durable results: `optimizer/campaigns/`,
  `optimizer/runs/` (synced to the Mac before teardown).
- The AMI and S3 bucket were deleted at teardown — rebuild via §2.
