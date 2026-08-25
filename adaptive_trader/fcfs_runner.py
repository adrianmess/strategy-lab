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
        # spot trades in base units — only futures needs a contract size
        cs = (None if cfg.get("mode") == "spot"
              else (cfg.get("contract_sizes") or {}).get(symbol))
        if cs:
            self.cfg["contract_size"] = float(cs)
        elif cfg.get("mode") != "spot":
            # inheriting the template's 0.1 (SOL) would mis-size by 1000x on
            # BTC — refuse rather than trade a wrong quantity
            raise SystemExit(f"no contract_size for {symbol} in this config's "
                             f"contract_sizes — re-adopt the combo")
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
        self.started_at = 0.0
        self.restarts = 0
        self.restart_at = 0.0      # scheduled (non-blocking) restart time

    def start(self):
        self.started_at = time.time()
        # Each host gets its OWN numba cache dir. With a shared cache, two
        # hosts compiling the same jitted function at the same time corrupt
        # the cache file for everyone; the next host to LOAD it segfaults in
        # numba's dispatcher (no traceback — the process just dies), and
        # lockstep restart backoffs then keep re-corrupting it forever.
        # 2026-08-23: ETH@3m + SUI@3m crash-looped for 2h exactly this way.
        nb_cache = os.path.join(HERE, ".numba_cache", self.key)
        os.makedirs(nb_cache, exist_ok=True)
        env = {**os.environ, "LAB_TF": str(self.tf_min),
               "NUMBA_CACHE_DIR": nb_cache}
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
        # ask MEXC which spot symbols THIS key may API-trade (the old
        # hardcoded BTC/ETH-only rule is stale — keys now get a wide list)
        from mexc_api import MexcFuturesAPI
        # configs store the account as api_account; "account" never existed,
        # so this silently validated mexc1 while trading another account
        allowed = set(MexcFuturesAPI(
            account=cfg.get("api_account") or "mexc1").spot_api_symbols())
        bad = {c["pair"] for c in comps
               if c["pair"].replace("_", "") not in allowed}
        if bad:
            raise SystemExit(f"this key's MEXC spot-API allowlist does not "
                             f"include {sorted(bad)} — enable them for the "
                             f"key or use another account. Dry-run works.")
    # PairExec reads cfg["mode"]; the authoritative value lives on the
    # candidate. A stale top-level "mode" would route a spot combo through
    # the futures executor.
    cfg["mode"] = mode
    log.info("FCFS live adapter starting: %d components, mode=%s, dry_run=%s",
             len(comps), mode, cfg["dry_run"])

    # manual close-override sidecar (written by the panel, polled by mtime)
    ov_path = os.path.join(HERE, ".override_" +
                           os.path.basename(cfg["state_file"]))
    ov = {"m": 0.0, "d": None}
    # adopt-shadow sidecar: the panel asks a FLAT trader to open a CHOSEN
    # component's virtual trade (deliberate close-and-switch, stage 2)
    ad_path = os.path.join(HERE, ".adopt_" +
                           os.path.basename(cfg["state_file"]))

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
               comp=comp_label(pos["comp"]),
               # carry the position so the trades table can show P&L
               position=dict(entry_price=pos.get("entry_price"),
                             qty=pos.get("qty"), lev=pos.get("lev"),
                             dir=pos.get("dir")),
               entry_price=pos.get("entry_price"), qty=pos.get("qty"),
               lev=pos.get("lev"),
               side=("LONG" if pos.get("dir", 1) > 0 else "SHORT"),
               live=(not cfg["dry_run"]), result=(res or {}).get("status"))
        if (res or {}).get("status") not in ("success", "dry_run"):
            notify("order_failed", account="fcfs", action="close",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=(res or {}).get("message"))
            # KEEP the position and the slot: clearing here would strand a
            # live position AND free the slot for a second one on top of it
            log.error("CLOSE FAILED (%s) — position kept, slot stays busy",
                      (res or {}).get("message"))
            return
        # A MANUAL/EMERGENCY close must STICK. Without this the late-join
        # sees the component's virtual trade still open, still "red", and
        # re-enters within a bar or two — exactly what happened on
        # 2026-08-17 (closed 124 @08:07:32, re-opened 119 @08:09:14).
        # armed_target is user-directed too: after it fires nothing may
        # auto-rejoin — the human (or a new armed rule) owns the next move
        if reason in ("manual_override", "emergency_exit", "close_now",
                      "armed_target"):
            sk = state.setdefault("late_skips", {})
            lbl = pos.get("mirror_entry_t")
            if lbl:
                sk[str(pos["comp"])] = lbl
                log.info("marking %s's virtual trade %s as skipped so the "
                         "late-join will not re-enter it",
                         comp_label(pos["comp"]), lbl)
            # A deliberate close means the HUMAN owns the next move: mark every
            # currently-open virtual trade skipped too, so no OTHER component
            # auto-joins into the freed slot seconds later. Adrian picks a
            # shadow explicitly (Adopt in the panel) or waits for fresh
            # signals — opens_now entries are untouched by late_skips.
            n_extra = 0
            for rows in shadow_by_key.values():
                for r in rows:
                    if str(r["comp"]) not in sk or sk[str(r["comp"])] != r["entry_t"]:
                        n_extra += 1
                    sk[str(r["comp"])] = r["entry_t"]
            if n_extra:
                log.info("manual close: %d other virtual trade(s) marked "
                         "skipped — nothing auto-joins; use Adopt to switch",
                         n_extra)
        state["position"] = None
        save(); tell_flat()

    # arbitration: same-bar ties resolved by component order (backtest rule)
    pending_opens = []          # [(bar_t, comp_i, dir, lev, px, group_key)]
    pending_deadline = 0.0

    # ---- SHADOW positions: what each component WOULD be holding ----------
    # The hosts keep evaluating every component whether or not the slot is
    # busy, so this is bookkeeping, not new computation. Written to the state
    # file so the panel can show "these trades exist virtually right now" —
    # the raw material for a deliberate close-and-switch.
    shadow_by_key = {}          # group key -> [entry, ...] from its last bar
    _shadow_sig = [None]        # membership signature of the last save
    _shadow_ts = [0.0]          # last save time (throttles price-only saves)

    def note_shadows(key, cbyi, px, now):
        sk = state.get("late_skips") or {}
        rows = []
        for ci, c in sorted(cbyi.items()):
            lbl = c.get("open")
            ep = c.get("entry_px")
            if lbl is None or not ep:
                continue
            if pos is not None and key == pos.get("group") \
                    and ci == pos.get("comp"):
                continue          # that IS the real position, not a shadow
            d = int(c.get("dir") or 1)
            lv = float(c.get("lev") or 1.0)
            pct = ((px / float(ep) - 1.0) * d * 100.0 *
                   (lv if mode == "lev" else 1.0)) if px else None
            rows.append(dict(comp=ci, label=comp_label(ci), group=key,
                             dir=d, lev=lv, entry_t=lbl,
                             entry_px=float(ep), px=px,
                             pct=round(pct, 3) if pct is not None else None,
                             skipped=(sk.get(str(ci)) == lbl)))
        shadow_by_key[key] = rows
        flat = [r for rs in shadow_by_key.values() for r in rs]
        sig = tuple(sorted((r["group"], r["comp"], r["entry_t"])
                           for r in flat))
        # save on membership change immediately; price refreshes at most
        # every 30s so twelve hosts do not turn ticks into disk churn
        if sig != _shadow_sig[0] or now - _shadow_ts[0] > 30:
            _shadow_sig[0] = sig
            _shadow_ts[0] = now
            state["shadow"] = flat
            state["shadow_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save()

    _ad_seen = [0.0]

    def try_adopt():
        """Panel-requested adopt of ONE chosen shadow (mtime-polled sidecar).
        Opens at the LIVE price; entry_price is OUR real fill (that is what
        P&L is measured from), the component's virtual entry is kept in
        mirror_entry_px for the 'what it would have been' display."""
        try:
            m = os.path.getmtime(ad_path)
        except OSError:
            return
        if m == _ad_seen[0]:
            return
        _ad_seen[0] = m
        try:
            req = json.load(open(ad_path))
        except Exception:
            req = None
        try:
            os.remove(ad_path)
        except OSError:
            pass
        if not req:
            return
        do_adopt(int(req.get("comp", -1)), req.get("entry_t"),
                 close_pct=req.get("close_pct"), src="panel")

    def do_adopt(ci, want, close_pct=None, src="panel"):
        """Open the chosen shadow as a REAL position (shared by the panel
        Adopt button and the armed auto-adopt rules)."""
        if state.get("position"):
            log.warning("ADOPT (%s) refused: a position is already open", src)
            return False
        row = None
        for rows in shadow_by_key.values():
            for r in rows:
                if r["comp"] == ci and r["entry_t"] == want:
                    row = r
        if row is None:
            log.warning("ADOPT refused: %s no longer holds virtual trade %s "
                        "(it may have just closed)", comp_label(ci), want)
            return False
        d = int(row["dir"])
        lv = float(row["lev"]) if mode == "lev" else 1.0
        ep = float(row["entry_px"])
        h = hosts.get(row["group"])
        px = float(getattr(h, "last_px", None) or row.get("px") or 0)
        if not px:
            log.warning("ADOPT refused: no live price for %s", row["group"])
            return False
        # keep the liq-distance guard — a deliberate switch must still not
        # inherit a virtual trade that is about to be liquidated
        if lv > 1:
            adv = (px / ep - 1.0) * d * -1.0
            cap = float(cfg.get("late_join_max_drawdown", 0.5))
            if (adv / max(1.0 / lv - 0.008, 1e-9)) > cap:
                log.warning("ADOPT refused %s: %.1f%% underwater at %gx — "
                            "too close to ITS liquidation",
                            comp_label(ci), 100 * adv, lv)
                return False
        res, qty = ex_for(comps[ci]["pair"]).open(d, lv, px)
        if (res or {}).get("status") == "error" or not qty:
            notify("order_failed", account="fcfs", action="adopt",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=(res or {}).get("message"))
            log.error("ADOPT open failed: %s", (res or {}).get("message"))
            return False
        fill = float((res or {}).get("fill_price") or px)
        state.setdefault("late_skips", {}).pop(str(ci), None)
        opened_bar[ci] = row["entry_t"]
        state["position"] = dict(
            symbol=comps[ci]["pair"], comp=ci, dir=d, lev=lv, qty=qty,
            entry_price=fill, group=row["group"],
            mirror_entry_t=row["entry_t"], mirror_entry_px=ep, adopted=True,
            armed_close_pct=(float(close_pct) if close_pct is not None
                             else None),
            opened_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            opened_ms=int(time.time() * 1000))
        log.info("ADOPTED (%s) %s: virtual entry %s @%.6g, joined @%.6g "
                 "dir=%+d lev=%.1f qty=%s close_at=%s", src, comp_label(ci),
                 row["entry_t"], ep, fill, d, lv, qty, close_pct)
        notify("position_opened", account="fcfs",
               config=os.path.basename(cfg.get("_path", "?")),
               symbol=comps[ci]["pair"], side=("LONG" if d > 0 else "SHORT"),
               qty=qty, lev=lv, price=fill,
               comp=comp_label(ci) + " (adopted)",
               live=(not cfg["dry_run"]))
        save(); tell_flat()
        return True

    # ---- ARMED shadow rules: adopt automatically when a chosen shadow's
    # unrealized falls to <= adopt_pct (usually negative); optionally close
    # the adopted position when OUR unrealized reaches >= close_pct.
    # Rules live in a panel-written sidecar so they survive restarts; a rule
    # fires ONCE and is removed; rules whose virtual trade ended are pruned.
    ar_path = os.path.join(HERE, ".armed_" +
                           os.path.basename(cfg["state_file"]))
    _ar = {"m": 0.0, "rules": []}

    def _armed_load():
        try:
            m = os.path.getmtime(ar_path)
        except OSError:
            if _ar["rules"]:
                _ar["rules"] = []
            return
        if m != _ar["m"]:
            _ar["m"] = m
            try:
                _ar["rules"] = json.load(open(ar_path)).get("rules") or []
                log.info("armed rules loaded: %d", len(_ar["rules"]))
            except Exception:
                _ar["rules"] = []

    def _armed_write():
        try:
            if _ar["rules"]:
                tmp = ar_path + ".tmp"
                json.dump(dict(rules=_ar["rules"]), open(tmp, "w"), indent=1)
                os.replace(tmp, ar_path)
            elif os.path.exists(ar_path):
                os.remove(ar_path)
            _ar["m"] = (os.path.getmtime(ar_path)
                        if os.path.exists(ar_path) else 0.0)
        except OSError:
            pass

    def check_armed():
        _armed_load()
        if not _ar["rules"] or state.get("position"):
            return
        keep = []
        changed = False
        for rule in _ar["rules"]:
            ci = int(rule.get("comp", -1))
            want = rule.get("entry_t")
            row = None
            for rows in shadow_by_key.values():
                for r in rows:
                    if r["comp"] == ci and r["entry_t"] == want:
                        row = r
            if row is None:
                if shadow_by_key:          # hosts reporting; trade truly gone
                    log.info("armed rule pruned: %s virtual trade %s ended",
                             comp_label(ci), want)
                    changed = True
                    continue
                keep.append(rule)
                continue
            pct = row.get("pct")
            if pct is not None and pct <= float(rule.get("adopt_pct", -1e9)):
                log.info("ARMED trigger: %s at %+.2f%% <= %.2f%%",
                         comp_label(ci), pct, float(rule["adopt_pct"]))
                do_adopt(ci, want, close_pct=rule.get("close_pct"),
                         src="armed")
                changed = True             # fired (or refused) — one shot
                continue
            keep.append(rule)
        if changed:
            _ar["rules"] = keep
            _armed_write()

    def check_armed_close():
        pos2 = state.get("position")
        cap = (pos2 or {}).get("armed_close_pct")
        if not pos2 or cap is None:
            return
        h = hosts.get(pos2.get("group"))
        px = float(getattr(h, "last_px", None) or 0)
        if not px:
            return
        d = int(pos2.get("dir") or 1)
        lv = float(pos2.get("lev") or 1.0) if mode == "lev" else 1.0
        pct = (px / float(pos2["entry_price"]) - 1.0) * d * 100.0 * lv
        if pct >= float(cap):
            log.info("ARMED close: %+.2f%% >= %.2f%% target", pct, float(cap))
            do_close("armed_target", px)

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
        # `px` is the last CLOSED bar's close, and we fire up to 1.5s later
        # (arbitration window) plus poll latency. Size and anchor on the LIVE
        # tick instead: entry_price is the denominator of the emergency-exit
        # net, the liq-proximity warning and the reported P&L.
        h = hosts.get(gkey)
        px_live = float(getattr(h, "last_px", None) or px)
        res, qty = ex_for(c["pair"]).open(d, lev, px_live)
        if (res or {}).get("status") == "error":
            notify("order_failed", account="fcfs", action="open",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=res.get("message"))
            return
        if qty and qty > 0:
            # prefer the venue's own fill price when the executor confirmed it
            px = float((res or {}).get("fill_price") or px_live)
            state["position"] = dict(
                symbol=c["pair"], comp=i, dir=d, lev=lev, qty=qty,
                entry_price=px, group=gkey,
                mirror_entry_t=None,   # filled below from the bar msg
                opened_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                opened_ms=int(time.time() * 1000))
            log.info("OPEN %s dir=%+d lev=%.1f qty=%s px=%.6g (signal bar %s)",
                     comp_label(i), d, lev, qty, px, bar_t)
            notify("position_opened", account="fcfs",
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=c["pair"], side=("LONG" if d > 0 else "SHORT"),
                   qty=qty, lev=lev, price=px, comp=comp_label(i),
                   live=(not cfg["dry_run"]))
            save(); tell_flat()
            return bar_t

    opened_bar = {}   # comp_i -> mirror entry_t recorded at open time

    tell_flat()
    last_note = 0
    # heartbeat: the loop is silent unless something happens, so a stalled
    # feed looks exactly like a quiet market. Every HB_EVERY seconds log what
    # each group last delivered, and flag any group whose bars stopped
    # arriving (> 4 bar intervals) — that is the actionable failure.
    HB_EVERY = 900
    last_hb = time.time()
    last_bar_at = {}      # group key -> (bar label, epoch received)
    bars_seen = [0]
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
                        # schedule it: sleeping here froze the emergency exit,
                        # liq-proximity check and manual override for up to a
                        # minute while a leveraged position was open
                        h.restart_at = now + wait
                elif e == "ready":
                    log.info("host %s ready (%s bars)", key, msg.get("bars"))
                elif e == "log":
                    log.info("[%s] %s", key, msg.get("msg"))
                elif e == "bar":
                    bt = msg.get("t")
                    last_bar_at[key] = (bt, now)
                    bars_seen[0] += 1
                    cbyi = {c["i"]: c for c in msg.get("comps", [])}
                    note_shadows(key, cbyi, float(msg.get("px") or 0), now)
                    if pos is None:
                        sk = state.setdefault("late_skips", {})
                        for ci, c in sorted(cbyi.items()):
                            if c.get("opens_now"):
                                opened_bar[ci] = c.get("open")
                                pending_opens.append(
                                    (bt, ci, int(c.get("dir") or 1),
                                     float(c.get("lev") or 1.0),
                                     float(msg.get("px") or 0), key))
                                continue
                            # LATE-JOIN: the component's virtual position was
                            # opened while we were down or otherwise flat.
                            # Join ONLY in the red (entering at or below the
                            # sim's price can only do better than the sim —
                            # a GREEN lev entry anchors liquidation at OUR
                            # worse price and can die on a drawdown the sim
                            # survives). Each virtual trade is judged ONCE:
                            # green -> sit it out, ignore its close, resume
                            # on the component's next fresh signal.
                            lbl = c.get("open")
                            if lbl is None:
                                if sk.pop(str(ci), None) is not None:
                                    save()            # that virtual trade ended
                                continue
                            if sk.get(str(ci)) == lbl:
                                continue              # judged green earlier
                            ep = c.get("entry_px")
                            px = float(msg.get("px") or 0)
                            d = int(c.get("dir") or 1)
                            if not ep or not px:
                                continue
                            # never join a trade the SIM is about to lose:
                            # we would inherit its forced exit (see the
                            # 2026-08-19 ETH short, sim 93% of the way to
                            # liquidation, mirrored exit cost ~$426)
                            adv = (px / ep - 1.0) * d * -1.0
                            lv = float(c.get("lev") or 1.0) if mode == "lev" \
                                else 1.0
                            cap = float(cfg.get("late_join_max_drawdown", 0.5))
                            too_deep = ((adv / max(1.0 / lv - 0.008, 1e-9)) > cap
                                        if lv > 1 else adv > cap * 0.2)
                            if too_deep:
                                sk[str(ci)] = lbl
                                save()
                                log.warning("LATE-JOIN REFUSED %s: sim entry "
                                            "%s @%.6g is %.1f%% underwater at "
                                            "%gx — too close to ITS "
                                            "liquidation",
                                            comp_label(ci), lbl, ep,
                                            100 * adv, lv)
                                continue
                            # red-only is the LEV exception; spot cannot
                            # liquidate, so spot combos join in any color
                            if (mode == "spot"
                                    or ((px < ep) if d > 0 else (px > ep))):
                                sk.pop(str(ci), None)
                                opened_bar[ci] = lbl
                                # bar_t = the VIRTUAL entry label, so FCFS
                                # time-priority lets older positions beat
                                # fresh signals in the same arbitration window
                                pending_opens.append(
                                    (lbl, ci, d, float(c.get("lev") or 1.0),
                                     px, key))
                                log.info("LATE-JOIN candidate %s: virtual "
                                         "entry %s @%.6g, now %.6g (red)",
                                         comp_label(ci), lbl, ep, px)
                            else:
                                sk[str(ci)] = lbl
                                save()
                                log.info("LATE-JOIN skipped %s: virtual entry "
                                         "%s @%.6g is GREEN at %.6g — waiting "
                                         "for its next fresh signal",
                                         comp_label(ci), lbl, ep, px)
                        if pending_opens and not pending_deadline:
                            pending_deadline = now + 1.5
                    else:
                        # position open: watch ONLY the owning component
                        if key == pos["group"] and pos["comp"] in cbyi:
                            me = cbyi[pos["comp"]]
                            # backfill the virtual entry price for positions
                            # opened before it was recorded (late-joins under
                            # the old code): powers the "virtual: ±x%" line
                            if (pos.get("mirror_entry_px") is None
                                    and me.get("entry_px")
                                    and me.get("open") == pos.get("mirror_entry_t")):
                                pos["mirror_entry_px"] = float(me["entry_px"])
                                save()
                            if pos.get("standalone"):
                                pass          # detached: no mirror to follow
                            elif pos.get("mirror_entry_t") is None:
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
                                # WHY did the virtual trade end? A LIQUIDATION
                                # is the death of a SIMULATED account, not a
                                # market signal — our real position was opened
                                # at a different price and is typically
                                # nowhere near its own liquidation. Mirroring
                                # it out just realises someone else's loss
                                # (2026-08-19: sim died at 2144 from a 1850
                                # entry; our short was opened at 2124 with
                                # liquidation ~2462 and was closed for -$413).
                                _why = me.get("last_reason")
                                _dead = (_why == "liquidation" and
                                         me.get("last_exit") == pos["mirror_entry_t"]
                                         or _why == "liquidation")
                                if _dead and not cfg.get(
                                        "mirror_sim_liquidation", False):
                                    log.warning(
                                        "%s: the SIM position liquidated — "
                                        "NOT mirroring that exit. Keeping our "
                                        "position (entry %.6g, our liq ~%.6g) "
                                        "and managing it standalone.",
                                        comp_label(pos["comp"]),
                                        pos["entry_price"],
                                        pos["entry_price"] * (
                                            1 + (1.0 / max(pos["lev"], 1e-9)
                                                 - 0.008) * -pos["dir"]))
                                    notify("sim_liquidated_position_kept",
                                           account="fcfs",
                                           config=os.path.basename(
                                               cfg.get("_path", "?")),
                                           symbol=pos["symbol"],
                                           comp=comp_label(pos["comp"]),
                                           entry=pos["entry_price"],
                                           price=msg.get("px"),
                                           live=(not cfg["dry_run"]))
                                    # detach from the dead mirror and give the
                                    # position its own exit plan, anchored to
                                    # OUR entry rather than the sim's
                                    pos["orphaned_at"] = time.strftime(
                                        "%Y-%m-%d %H:%M:%S")
                                    pos["mirror_entry_t"] = None
                                    pos["standalone"] = True
                                    sk = state.setdefault("late_skips", {})
                                    sk[str(pos["comp"])] = me.get("open") or ""
                                    save()
                                else:
                                    do_close("virtual_exit", msg.get("px"))

            # panel-requested adopt of a chosen shadow (only when flat) +
            # armed auto-adopt / auto-close rules
            if state.get("position") is None:
                try_adopt()
                check_armed()
            else:
                check_armed_close()

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
                    # ---- STANDALONE position (its sim liquidated) ----
                    # It has no mirror left to follow, so it gets an explicit
                    # plan anchored to OUR entry: take the component's own
                    # edge if it arrives, and cap the downside — never leave
                    # a stopless leveraged position drifting.
                    elif pos.get("standalone"):
                        tp = float(cfg.get("standalone_take_profit", 0.005))
                        liq_d = (1.0 / max(pos["lev"], 1e-9) - 0.008
                                 if mode == "lev" else 1.0)
                        sl_frac = float(cfg.get("standalone_stop_frac", 0.5))
                        if adverse >= tp:
                            log.info("STANDALONE take-profit hit (+%.2f%% "
                                     "from our entry)", 100 * adverse)
                            do_close("standalone_take_profit", px)
                        elif adverse <= -abs(sl_frac * liq_d):
                            log.warning("STANDALONE stop hit (%.2f%% adverse, "
                                        "%.0f%% of our liquidation distance)",
                                        100 * -adverse, 100 * sl_frac)
                            do_close("standalone_stop", px)
                    # manual override: panel-set trigger price for THIS
                    # position (sidecar file, mtime-polled — no restart)
                    try:
                        m = os.path.getmtime(ov_path)
                    except OSError:
                        m = 0
                        ov["d"] = None
                    if m and m != ov["m"]:
                        ov["m"] = m
                        try:
                            ov["d"] = json.load(open(ov_path))
                        except Exception:
                            ov["d"] = None
                    d_ = ov["d"]
                    if (d_ and str(pos.get("opened_at")) == d_.get("pos_key")
                            and (d_.get("now")
                                 or ((px >= d_["price"]) if d_.get("above")
                                     else (px <= d_["price"])))):
                        log.warning("MANUAL %s close at %.6g",
                                    "CLOSE-NOW" if d_.get("now") else
                                    f"OVERRIDE (trigger {d_['price']:.6g})",
                                    px)
                        try:
                            os.remove(ov_path)
                        except OSError:
                            pass
                        ov["d"] = None
                        do_close("manual_override", px)

            # due host restarts (scheduled, never blocking — see "died")
            for _k, _h in hosts.items():
                if _h.restart_at and now >= _h.restart_at and not _h.alive():
                    _h.restart_at = 0.0
                    log.info("restarting host %s now", _k)
                    _h.start()
                    tell_flat()

            # watchdog: a silent host while we hold ITS position is a hazard
            if pos:
                h = hosts.get(pos["group"])
                if h and h.alive() and time.time() - h.last_seen > 300:
                    log.warning("host %s silent >5min while positioned",
                                pos["group"])

            # periodic heartbeat + stale-feed detection
            if now - last_hb >= HB_EVERY:
                last_hb = now
                stale = []
                for k, hst in hosts.items():
                    tf = hst.tf_min
                    lb = last_bar_at.get(k)
                    # No bar yet? Age from host START — error chatter must not
                    # reset the clock (a host stuck retrying its backfill sat
                    # dark for hours on 08-12 without ever looking stale).
                    age = (now - lb[1]) if lb else (now - hst.started_at)
                    if age > max(240, tf * 60 * 4):
                        stale.append(f"{k} ({age/60:.0f}m"
                                     + ("" if lb else ", no bars yet") + ")")
                where = (f"{pos['symbol']} via {comp_label(pos['comp'])}"
                         if pos else "empty")
                (log.warning if stale else log.info)(
                    "heartbeat: slot=%s | %d/%d hosts alive | %d bars "
                    "evaluated in the last %dm%s", where,
                    sum(1 for h in hosts.values() if h.alive()),
                    len(hosts), bars_seen[0], HB_EVERY // 60,
                    (" | STALE FEEDS: " + ", ".join(stale)) if stale
                    else "")
                bars_seen[0] = 0
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
