#!/usr/bin/env python3
"""FCFS live adapter — PARENT. One shared position slot over N component
strategies spread across any pairs/timeframes (2..N components).

Semantics (mirrors optimizer/fcfsx_cli.py, the backtested merge):
  - every component runs virtually, engine-exact, inside per-(pair,tf) host
    subprocesses (fcfs_host.py; one process per timeframe group because the
    research engines read LAB_TF at import);
  - when the slot is FREE and a component's virtual trade OPENS on a just-
    closed bar, the real position opens on that component's pair at its
    leverage — first signal wins; simultaneous signals (same bar close) break
    ties by component order in the config, matching the backtest tiebreak;
  - the real position closes when the mirrored virtual trade closes;
  - optional emergency_exit_adverse acts as a global intra-bar safety net.

Config = a regular trader config whose candidate is:
  {"strategy": "fcfsx", "mode": "lev"|"spot",
   "components": [{"strategy","method","cand","run",
                   "pair": "BTC_USDT", "timeframe": "3m"}, ...]}
plus "contract_sizes": {"BTC_USDT": 0.0001, ...} for futures sizing.

Run through trader.py (it delegates here) so the panel's instance machinery,
dry-run default and --live flag all work unchanged. DRY RUN logs orders
without touching the exchange, exactly like the single-pair trader.
"""
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from notify import notify                                    # noqa: E402

log = logging.getLogger("fcfs")
LIVE_FAMS = ("macdx", "scalpx", "scalpx2", "v7", "prime7", "prime", "v6")


# ---------------- per-pair executors (dry-run aware) ----------------
class PairExec:
    """Wraps the existing executor classes with a per-pair cfg copy."""
    def __init__(self, cfg, symbol):
        self.cfg = dict(cfg)
        self.cfg["symbol"] = symbol
        cs = (cfg.get("contract_sizes") or {}).get(symbol)
        if cs:
            self.cfg["contract_size"] = float(cs)
        from trader import Executor, APIExecutor, APISpotExecutor
        if cfg.get("execution") == "api":
            self.ex = (APISpotExecutor(self.cfg) if cfg.get("mode") == "spot"
                       else APIExecutor(self.cfg))
        else:
            self.ex = Executor(self.cfg)

    def open(self, direction, lev, price):
        return self.ex.open_position(direction, lev, price)

    def close(self):
        return self.ex.close_position()


# ---------------- host management ----------------
class Host:
    def __init__(self, key, symbol, tf_min, mode, comps, poll, q):
        self.key, self.symbol, self.tf_min = key, symbol, tf_min
        self.mode, self.comps, self.poll, self.q = mode, comps, poll, q
        self.proc = None
        self.last_px = None
        self.last_seen = 0.0
        self.restarts = 0

    def start(self):
        env = {**os.environ, "LAB_TF": str(self.tf_min)}
        errlog = open(os.path.join(HERE, f".fcfs_host_{self.key}.err"), "a")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "fcfs_host.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errlog, cwd=HERE, env=env, text=True)
        spec = dict(symbol=self.symbol, tf_min=self.tf_min, mode=self.mode,
                    poll_seconds=self.poll,
                    components=[dict(i=c["_i"], strategy=c["strategy"],
                                     method=c.get("method", "vol3"),
                                     cand=c["cand"], run=c.get("run", "?"))
                                for c in self.comps])
        self.proc.stdin.write(json.dumps(spec) + "\n")
        self.proc.stdin.flush()
        threading.Thread(target=self._pump, daemon=True).start()
        log.info("host %s started (pid %d, %d comps)",
                 self.key, self.proc.pid, len(self.comps))

    def _pump(self):
        p = self.proc
        for line in p.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            self.last_seen = time.time()
            if msg.get("e") == "px":
                self.last_px = msg.get("px")
            self.q.put((self.key, msg))
        self.q.put((self.key, {"e": "died"}))

    def set_flat(self, flat):
        try:
            self.proc.stdin.write(json.dumps({"flat": bool(flat)}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


# ---------------- main ----------------
def main_fcfs(cfg, live):
    if live:
        cfg["dry_run"] = False
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(HERE, cfg["log_file"]))])
    cand = cfg["candidate"]
    comps = cand["components"]
    mode = cand.get("mode") or cfg.get("mode") or "lev"
    if len(comps) < 2:
        raise SystemExit("fcfsx live needs >= 2 components")
    for i, c in enumerate(comps):
        c["_i"] = i
        if c["strategy"] not in LIVE_FAMS:
            raise SystemExit(f"component {i} family '{c['strategy']}' has no "
                             f"live runner")
        if not c.get("pair") or not c.get("timeframe"):
            raise SystemExit(f"component {i} missing pair/timeframe")
    if mode == "spot" and not cfg["dry_run"] and cfg.get("execution") == "api":
        bad = {c["pair"] for c in comps} - {"BTC_USDT", "ETH_USDT"}
        if bad:
            raise SystemExit(f"LIVE spot via API is restricted to BTC/ETH on "
                             f"MEXC — pairs {sorted(bad)} can't trade live "
                             f"spot. Dry-run works; lev mode works.")
    log.info("FCFS live adapter starting: %d components, mode=%s, dry_run=%s",
             len(comps), mode, cfg["dry_run"])

    # state
    sf = os.path.join(HERE, cfg["state_file"])
    state = json.load(open(sf)) if os.path.exists(sf) else {}
    state.setdefault("position", None)   # {symbol, comp, dir, lev, qty,
    #                                       entry_price, mirror_entry_t, group}
    def save():
        tmp = sf + ".tmp"
        json.dump(state, open(tmp, "w"), indent=2, default=float)
        os.replace(tmp, sf)

    # groups
    q = queue.Queue()
    groups = {}
    for c in comps:
        key = f"{c['pair']}@{int(str(c['timeframe']).rstrip('m'))}m"
        groups.setdefault(key, []).append(c)
    hosts = {}
    for n, (key, cs) in enumerate(groups.items()):
        sym, tf = key.split("@")
        hosts[key] = Host(key, sym, int(tf.rstrip("m")), mode, cs,
                          cfg.get("poll_seconds", 3), q)
        hosts[key].start()
        if n < len(groups) - 1:
            time.sleep(8)      # stagger backfills — one shared IP for klines
    execs = {}
    def ex_for(sym):
        if sym not in execs:
            execs[sym] = PairExec(cfg, sym)
        return execs[sym]

    def comp_label(i):
        c = comps[i]
        return f"#{i} {c['pair']}/{c['timeframe']}·{c['strategy']}"

    def tell_flat():
        flat = state.get("position") is None
        for h in hosts.values():
            h.set_flat(flat)

    def do_close(reason, px=None):
        pos = state.get("position")
        if not pos:
            return
        res = ex_for(pos["symbol"]).close()
        log.info("CLOSE %s (%s): %s", comp_label(pos["comp"]), reason,
                 (res or {}).get("status"))
        notify("position_closed", account="fcfs",
               config=os.path.basename(cfg.get("_path", "?")),
               symbol=pos["symbol"], reason=reason, price=px,
               live=(not cfg["dry_run"]), result=(res or {}).get("status"))
        if (res or {}).get("status") == "error":
            notify("order_failed", account="fcfs", action="close",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=res.get("message"))
        state["position"] = None
        save(); tell_flat()

    # arbitration: same-bar ties resolved by component order (backtest rule)
    pending_opens = []          # [(bar_t, comp_i, dir, lev, px, group_key)]
    pending_deadline = 0.0

    def flush_pending():
        nonlocal pending_opens, pending_deadline
        if not pending_opens or state.get("position"):
            pending_opens = []
            return
        pending_opens.sort(key=lambda x: (x[0], x[1]))   # (bar time, comp idx)
        bar_t, i, d, lev, px, gkey = pending_opens[0]
        pending_opens = []
        c = comps[i]
        if mode != "lev":
            lev = 1.0
        res, qty = ex_for(c["pair"]).open(d, lev, px)
        if (res or {}).get("status") == "error":
            notify("order_failed", account="fcfs", action="open",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=res.get("message"))
            return
        if qty and qty > 0:
            state["position"] = dict(
                symbol=c["pair"], comp=i, dir=d, lev=lev, qty=qty,
                entry_price=px, group=gkey,
                mirror_entry_t=None,   # filled below from the bar msg
                opened_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            log.info("OPEN %s dir=%+d lev=%.1f qty=%s px=%.6g (first signal)",
                     comp_label(i), d, lev, qty, px)
            notify("position_opened", account="fcfs",
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=c["pair"], side=("LONG" if d > 0 else "SHORT"),
                   qty=qty, lev=lev, price=px, live=(not cfg["dry_run"]))
            save(); tell_flat()
            return bar_t

    opened_bar = {}   # comp_i -> mirror entry_t recorded at open time

    tell_flat()
    last_note = 0
    while True:
        try:
            try:
                key, msg = q.get(timeout=1.0)
            except queue.Empty:
                key, msg = None, None

            now = time.time()
            pos = state.get("position")

            if msg is not None:
                e = msg.get("e")
                if e == "died":
                    h = hosts[key]
                    if h.restarts < 200:
                        h.restarts += 1
                        wait = min(60, 5 * h.restarts)
                        log.warning("host %s died — restart #%d in %ds",
                                    key, h.restarts, wait)
                        time.sleep(wait)
                        h.start(); tell_flat()
                elif e == "ready":
                    log.info("host %s ready (%s bars)", key, msg.get("bars"))
                elif e == "log":
                    log.info("[%s] %s", key, msg.get("msg"))
                elif e == "bar":
                    bt = msg.get("t")
                    cbyi = {c["i"]: c for c in msg.get("comps", [])}
                    if pos is None:
                        for ci, c in sorted(cbyi.items()):
                            if c.get("opens_now"):
                                opened_bar[ci] = c.get("open")
                                pending_opens.append(
                                    (bt, ci, int(c.get("dir") or 1),
                                     float(c.get("lev") or 1.0),
                                     float(msg.get("px") or 0), key))
                        if pending_opens and not pending_deadline:
                            pending_deadline = now + 1.5
                    else:
                        # position open: watch ONLY the owning component
                        if key == pos["group"] and pos["comp"] in cbyi:
                            me = cbyi[pos["comp"]]
                            if pos.get("mirror_entry_t") is None:
                                # first bar msg after open: bind the mirror
                                bind = me.get("open") or opened_bar.get(pos["comp"])
                                if bind is None:
                                    # virtual trade already closed again —
                                    # mirror it out immediately
                                    do_close("virtual_exit_fast", msg.get("px"))
                                else:
                                    state["position"]["mirror_entry_t"] = bind
                                    save()
                            elif me.get("open") != pos["mirror_entry_t"]:
                                do_close("virtual_exit", msg.get("px"))

            # arbitration window expired?
            if pending_deadline and now >= pending_deadline:
                pending_deadline = 0.0
                bar_t = flush_pending()
                if bar_t and state.get("position") is not None:
                    p = state["position"]
                    if p.get("mirror_entry_t") is None:
                        p["mirror_entry_t"] = opened_bar.get(p["comp"])
                        save()

            # protective intra-bar checks on the live price of the open pair
            pos = state.get("position")
            if pos:
                h = hosts.get(pos["group"])
                px = h.last_px if h else None
                if px:
                    adverse = (px / pos["entry_price"] - 1.0) * pos["dir"]
                    if mode == "lev":
                        liq_dist = 1.0 / max(pos["lev"], 1e-9) - 0.008
                        if adverse <= -0.5 * liq_dist and now - last_note > 300:
                            last_note = now
                            log.warning("LIQ PROXIMITY %s: adverse %.2f%% "
                                        "(liq at %.2f%%)",
                                        comp_label(pos["comp"]),
                                        100 * -adverse, 100 * liq_dist)
                    em = cfg.get("emergency_exit_adverse")
                    if em and adverse <= -abs(em):
                        do_close("emergency_exit", px)

            # watchdog: a silent host while we hold ITS position is a hazard
            if pos:
                h = hosts.get(pos["group"])
                if h and h.alive() and time.time() - h.last_seen > 300:
                    log.warning("host %s silent >5min while positioned",
                                pos["group"])
        except KeyboardInterrupt:
            log.info("stopped by user")
            for h in hosts.values():
                try:
                    h.proc.terminate()
                except Exception:
                    pass
            break
        except Exception as ex:
            log.exception("fcfs loop error: %s", ex)
            notify("trader_error", account="fcfs",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=str(ex)[:300])
            time.sleep(15)
