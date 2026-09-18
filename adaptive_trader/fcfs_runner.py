#!/usr/bin/env python3
"""FCFS live adapter — PARENT. One shared position slot over N component
strategies spread across any pairs/timeframes (2..N components) — or, with
"cascade": true in the config, a CAPITAL POOL: every fresh signal opens
while free capital remains (one position per symbol, each entry capped by
the liquidity guardrail), validated in optimizer/cascade_sim.py 2026-08-28.

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

    def open(self, direction, lev, price, margin_cap=None, entry_limit=None):
        if entry_limit is not None:
            return self.ex.open_position(direction, lev, price,
                                         margin_cap=margin_cap,
                                         entry_limit=entry_limit)
        return self.ex.open_position(direction, lev, price,
                                     margin_cap=margin_cap)

    def entry_state(self, order_id, direction, tries=1):
        """('resting'|'filled'|'gone', holdVol, holdAvgPrice) or None
        (unknown). Filled = a position in our direction is actually held —
        the runner enforces one position per symbol, so the hold IS ours.

        'gone' is the dangerous verdict: it tells the caller to forget a real
        order. Fills settle ASYNCHRONOUSLY on MEXC, so a single position read
        taken moments after placing or cancelling can legitimately show
        nothing while the fill is in flight — which is how 6,363 SUI got
        opened, declared 'gone' 7s later and left untracked (2026-09-17).
        `tries` re-reads before conceding; the timeout path uses it."""
        if order_id == "dry":
            return ("resting", None, None)
        last = None
        for i in range(max(1, tries)):
            if i:
                time.sleep(0.6 * i)        # same settle budget as _confirm_fill
            try:
                oo = self.ex.api.open_orders(self.cfg["symbol"]) or []
                if any(str(o.get("orderId")) == str(order_id) for o in oo):
                    return ("resting", None, None)
                want = 1 if direction > 0 else 2
                for p in (self.ex.api.open_positions(self.cfg["symbol"]) or []):
                    if (int(p.get("positionType") or 0) == want
                            and float(p.get("holdVol") or 0) > 0):
                        return ("filled", float(p["holdVol"]),
                                float(p.get("holdAvgPrice") or 0) or None)
                last = ("gone", None, None)
            except Exception:
                last = None
        return last

    def close(self):
        return self.ex.close_position()

    # ---- resting reduce-only take-profit (config: resting_tp) ----
    def place_tp(self, direction, qty, price):
        """Rest a close-side limit at `price`. Returns order id, 'dry' in
        dry-run, None on failure/unsupported (webhook executor)."""
        if self.cfg.get("dry_run"):
            return "dry"
        fn = getattr(self.ex, "place_tp", None)
        if fn is None:
            return None
        try:
            return (fn(qty, price) if self.cfg.get("mode") == "spot"
                    else fn(qty, price, direction))
        except Exception:
            return None

    def cancel_tp(self, order_id):
        if order_id in (None, "dry"):
            return True
        fn = getattr(self.ex, "cancel_tp", None)
        try:
            return bool(fn(order_id)) if fn else False
        except Exception:
            return False

    def tp_state(self, order_id):
        """'open' | 'filled' | 'gone' | None (unknown) for a resting TP.
        Futures: on the book = open; off the book + position flat = filled;
        off the book + still held = gone (externally cancelled). Spot: the
        order's own status."""
        if order_id == "dry":
            return "open"
        try:
            if self.cfg.get("mode") == "spot":
                st = (self.ex.tp_status(order_id) or {})
                s = str(st.get("status") or "").upper()
                if s in ("NEW", "PARTIALLY_FILLED"):
                    return "open"
                if s == "FILLED":
                    return "filled"
                return "gone" if s else None
            oo = self.ex.api.open_orders(self.cfg["symbol"]) or []
            if any(str(o.get("orderId")) == str(order_id) for o in oo):
                return "open"
            held = any(float(p.get("holdVol") or 0) > 0 for p in
                       (self.ex.api.open_positions(self.cfg["symbol"]) or []))
            return "gone" if held else "filled"
        except Exception:
            return None


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
    if len(comps) < 1:
        raise SystemExit("fcfsx live needs >= 1 component")
    # 1-component configs are legitimate: the adopt endpoint wraps standalone
    # macdx/scalpx/scalpx2 candidates (no standalone adapter) in a one-slot
    # FCFS router so they run engine-exact (MEX 2 Lev, 2026-09-15).
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

    # ---- CASCADE (multi-slot): validated in optimizer/cascade_sim.py
    # (2026-08-28). Instead of ONE shared slot, every fresh signal opens
    # while free capital remains — the liquidity guardrail caps each pair,
    # and what a capped position leaves behind funds the next signal.
    # OFF by default: flag off = the exact one-slot FCFS behavior.
    # Adoption, armed rules and late-join stay FLAT-ONLY in both modes
    # (the sim validated fresh-signal cascading only).
    cascade = bool(cfg.get("cascade"))
    max_slots = int(cfg.get("cascade_max_slots") or 0)     # 0 = unlimited
    if cascade and mode == "lev":
        _missing = sorted({c["pair"] for c in comps
                           if not (cfg.get("contract_sizes") or {})
                           .get(c["pair"])})
        if _missing:
            # ex_for() raises on a missing contract_size; under cascade that
            # would kill the loop mid-flight — refuse at startup instead
            raise SystemExit(f"cascade needs contract_sizes for every "
                             f"component pair — missing {_missing}")
    log.info("FCFS live adapter starting: %d components, mode=%s, dry_run=%s, "
             "cascade=%s%s", len(comps), mode, cfg["dry_run"], cascade,
             (f" (max_slots={max_slots})" if max_slots else ""))

    # manual close-override sidecar (written by the panel, polled by mtime)
    ov_path = os.path.join(HERE, ".override_" +
                           os.path.basename(cfg["state_file"]))
    ov = {"m": 0.0, "d": None}
    # adopt-shadow sidecar: the panel asks a FLAT trader to open a CHOSEN
    # component's virtual trade (deliberate close-and-switch, stage 2)
    ad_path = os.path.join(HERE, ".adopt_" +
                           os.path.basename(cfg["state_file"]))
    # PAUSE sidecar (panel-written, mtime-polled — no restart): while it
    # exists, NO new position is opened (fresh signals, late-join, adopt,
    # armed/auto rules, cascade slots) but every OPEN position keeps being
    # managed exactly as before — mirror exits, TP/SL, emergency exit,
    # manual overrides all still fire. Wind-down mode.
    pz_path = os.path.join(HERE, ".pause_" +
                           os.path.basename(cfg["state_file"]))
    _pz = {"m": -1.0, "on": False}

    def paused():
        try:
            m = os.path.getmtime(pz_path)
        except OSError:
            m = 0.0
        if m != _pz["m"]:
            was = _pz["on"]
            _pz["m"] = m
            _pz["on"] = bool(m)
            if _pz["on"] != was:
                log.warning("PAUSE %s", "ON — open positions still managed "
                            "to close; no NEW entries" if _pz["on"]
                            else "OFF — entries resume")
        return _pz["on"]

    # state — positions is a LIST (cascade holds several at once; one-slot
    # mode simply never grows it past 1). Legacy single "position" migrates
    # in on load and is kept as a MIRROR of positions[0] on every save so
    # older panel readers keep working.
    sf = os.path.join(HERE, cfg["state_file"])
    state = json.load(open(sf)) if os.path.exists(sf) else {}
    if state.get("positions") is None:
        state["positions"] = ([state["position"]]
                              if state.get("position") else [])
    positions = state["positions"]       # mutate in place, NEVER rebind
    # each: {symbol, comp, dir, lev, qty, entry_price, mirror_entry_t,
    #        group, margin?, ...}

    def save():
        state["position"] = positions[0] if positions else None
        tmp = sf + ".tmp"
        json.dump(state, open(tmp, "w"), indent=2, default=float)
        os.replace(tmp, sf)

    def pos_of_comp(ci):
        for p in positions:
            if p.get("comp") == ci:
                return p
        return None

    # resting entry limits awaiting fill (limit_entry) — persisted so a
    # restart resumes tracking the orders it left on the book
    pending_entries = state.setdefault("pending_entries", [])

    def sym_held(sym):
        # ONE position per symbol: exchange-side, same-symbol positions
        # merge (futures) / share a wallet balance (spot), so a second
        # "position" on a held pair could not be closed independently.
        # A RESTING entry order counts — it may fill any moment.
        return (any(p.get("symbol") == sym for p in positions)
                or any(pe.get("symbol") == sym for pe in pending_entries))

    def cascade_free():
        """Free margin available to the NEXT allocation, or None when the
        executor should size itself. Dry-run cascade: a paper tracker
        (equity_usdt minus margin committed to open paper positions, plus
        realized paper P&L) — without it every dry position would size from
        the same full paper balance. Live cascade: None — the exchange's
        available balance already shrinks as margin is committed, which IS
        the cascade allocation."""
        if not cascade or not cfg["dry_run"]:
            return None
        if state.get("cascade_free") is None:
            state["cascade_free"] = float(cfg.get("equity_usdt") or 0)
        return float(state["cascade_free"])

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
        # include the method: "v7" alone is ambiguous — the family can run
        # as trend3/vol3/etc., and comparing against the wrong candidate
        # cost a debugging session (2026-08-31, DOGE zombie shadow)
        c = comps[i]
        m = c.get("method")
        return (f"#{i} {c['pair']}/{c['timeframe']}·{c['strategy']}"
                + (f"·{m}" if m else ""))

    def tell_flat():
        flat = not positions and not pending_entries
        for h in hosts.values():
            h.set_flat(flat)

    def do_close(pos, reason, px=None):
        if pos is None or pos not in positions:
            return
        # cancel the resting TP FIRST so it cannot double-fire against the
        # market close below; a cancel on an already-filled order no-ops
        if pos.get("tp_order_id"):
            ex_for(pos["symbol"]).cancel_tp(pos["tp_order_id"])
            pos["tp_order_id"] = None
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
        if reason == "resting_tp_fill" and pos.get("mirror_entry_t"):
            # the exchange closed us at the target but the VIRTUAL trade may
            # still be open — mark it skipped so late-join can't re-enter the
            # same trade we just banked (only THIS trade; nothing else)
            state.setdefault("late_skips", {})[str(pos["comp"])] = \
                pos["mirror_entry_t"]
        if pos.get("auto"):
            # anti-churn memory for the standing auto-adopt rule: the same
            # virtual trade is only re-adopted >=1% below this exit
            state["auto_last"] = dict(comp=pos.get("comp"),
                                      entry_t=pos.get("mirror_entry_t"),
                                      exit_px=(px or pos.get("entry_price")))
        positions.remove(pos)
        # dry-run cascade paper accounting: return the margin + paper P&L
        # to the free pool (fees ignored — the soak measures behavior)
        if cascade and cfg["dry_run"] and pos.get("margin"):
            m = float(pos["margin"])
            ent = float(pos.get("entry_price") or 0)
            lv = float(pos.get("lev") or 1.0) if mode == "lev" else 1.0
            r = ((float(px) / ent - 1.0) * int(pos.get("dir") or 1) * lv
                 if px and ent else 0.0)
            state["cascade_free"] = round(
                float(state.get("cascade_free") or 0)
                + m * (1.0 + max(r, -1.0)), 4)
            log.info("CASCADE paper close: margin %.2f r=%+.2f%% -> free "
                     "%.2f", m, 100 * r, state["cascade_free"])
        save(); tell_flat()

    # ---- resting reduce-only TP limits (config: resting_tp, default off) --
    # When ON, every open position keeps ONE close-side limit resting at the
    # engine's projected profit target (exit_proj.tp, refreshed per bar —
    # macdx/v6/v7 targets DECAY over time, so the order is cancel/replaced
    # when the target moves). Fills are MAKER (0 fee on MEXC futures) at the
    # exact target instead of taker at the next bar open.
    rtp_on = bool(cfg.get("resting_tp"))
    RTP_EPS = float(cfg.get("resting_tp_eps", 0.0005))
    if rtp_on:
        log.info("RESTING TP ON: close-side limits at each position's "
                 "projected target (re-placed when it moves >%.2f%%)",
                 100 * RTP_EPS)

    def rtp_sync(pos):
        """Place/refresh the resting TP for one position."""
        if not rtp_on or pos not in positions:
            return
        tp = float((pos.get("exit_proj") or {}).get("tp") or 0)
        ep = float(pos.get("entry_price") or 0)
        if tp <= 0 or ep <= 0:
            return
        # sanity: a target on the WRONG side of entry would close at a loss
        # the moment it rests — refuse and log rather than trade it
        if (tp <= ep) if pos.get("dir", 1) > 0 else (tp >= ep):
            if not pos.get("_tp_side_warned"):
                pos["_tp_side_warned"] = True
                log.warning("resting TP for %s skipped: projected target "
                            "%.6g is on the wrong side of entry %.6g "
                            "(decayed below entry?) — engine exit will "
                            "handle this trade", comp_label(pos["comp"]),
                            tp, ep)
            return
        cur = pos.get("tp_px")
        if pos.get("tp_order_id") and cur and abs(tp - cur) / cur <= RTP_EPS:
            return                       # unchanged — leave the order alone
        ex = ex_for(pos["symbol"])
        if pos.get("tp_order_id"):
            ex.cancel_tp(pos["tp_order_id"])
            pos["tp_order_id"] = None
        oid = ex.place_tp(int(pos.get("dir") or 1), pos["qty"], tp)
        if oid:
            pos["tp_order_id"], pos["tp_px"] = oid, float(tp)
            pos.pop("_tp_side_warned", None)
            log.info("RESTING TP %s: %s qty %s @ %.6g (order %s)",
                     comp_label(pos["comp"]),
                     "SELL" if pos.get("dir", 1) > 0 else "BUY",
                     pos["qty"], tp, oid)
        else:
            pos.pop("tp_px", None)
        save()

    # ---- maker (limit) entries (config: limit_entry, default off) ----
    # Signal opens rest a POST-ONLY limit at the live price (± offset)
    # instead of paying taker: filled -> position at the limit price (maker,
    # 0 fee); unfilled after limit_entry_timeout_s -> cancel, then chase
    # with a market order (limit_entry_on_timeout: "market", default) or
    # skip the trade ("cancel"). Panel Adopts stay MARKET — a deliberate
    # human action should fill now, not maybe. Futures only for now.
    le_on = bool(cfg.get("limit_entry")) and mode == "lev"
    if cfg.get("limit_entry") and mode != "lev":
        log.warning("limit_entry: spot not supported yet — market entries")
    LE_TIMEOUT = float(cfg.get("limit_entry_timeout_s", 75))
    LE_CHASE = str(cfg.get("limit_entry_on_timeout", "market")).lower()
    LE_OFF = float(cfg.get("limit_entry_offset_bps", 0)) / 10000.0
    if le_on:
        log.info("LIMIT ENTRY ON: post-only at live px %+.1f bps, timeout "
                 "%.0fs -> %s", -1e4 * LE_OFF, LE_TIMEOUT, LE_CHASE)

    def _pe_refund(pe):
        if cascade and cfg["dry_run"] and pe.get("margin") is not None:
            state["cascade_free"] = round(
                float(state.get("cascade_free") or 0) + float(pe["margin"]), 4)

    def _mk_posn(pe, qty, fpx, chased=False):
        posn = dict(symbol=pe["symbol"], comp=pe["comp"], dir=pe["dir"],
                    lev=pe["lev"], qty=qty, entry_price=float(fpx),
                    group=pe["group"], mirror_entry_t=None,
                    late_join=(pe.get("src") == "late_join"),
                    limit_entry=(not chased),
                    opened_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    opened_ms=int(time.time() * 1000))
        positions.append(posn)
        opened_bar[pe["comp"]] = pe.get("bar_t")
        notify("position_opened", account="fcfs",
               config=os.path.basename(cfg.get("_path", "?")),
               symbol=pe["symbol"],
               side=("LONG" if pe["dir"] > 0 else "SHORT"),
               qty=qty, lev=pe["lev"], price=float(fpx),
               comp=comp_label(pe["comp"])
               + (" (limit chase)" if chased else " (limit entry)"),
               live=(not cfg["dry_run"]))
        return posn

    def _entry_filled(pe, vol, fpx):
        pending_entries.remove(pe)
        posn = _mk_posn(pe, vol or pe["qty"], fpx or pe["limit_px"])
        if pe.get("margin") is not None:
            posn["margin"] = pe["margin"]     # committed at placement
        log.info("ENTRY LIMIT FILLED %s: qty %s @ %.6g (maker)",
                 comp_label(pe["comp"]), posn["qty"], posn["entry_price"])
        save(); tell_flat()
        rtp_sync(posn)

    def _entry_timeout(pe, why=""):
        exx = ex_for(pe["symbol"])
        cancelled = exx.cancel_tp(pe["oid"])   # same cancel endpoint
        if pe["oid"] != "dry":
            # NEVER drop a pending entry we haven't proven is dead. The old
            # code cancelled, glanced once at the positions and dropped it
            # regardless — so a cancel that silently failed left a live
            # post-only order on the book that the runner had forgotten.
            # It filled seconds later: 6,363 SUI, no TP, no stop, no
            # tracking, found only because the price happened to run our
            # way (2026-09-17).
            st_ = exx.entry_state(pe["oid"], pe["dir"], tries=3)
            if st_ and st_[0] == "filled":
                _entry_filled(pe, st_[1], st_[2])
                return
            if st_ is None or st_[0] == "resting":
                # still on the book, or the exchange won't answer — keep
                # tracking and try again next round rather than abandon it
                pe["cancel_fails"] = int(pe.get("cancel_fails", 0)) + 1
                pe["checked"] = time.time()
                if pe["cancel_fails"] in (1, 3, 10):
                    log.error("ENTRY LIMIT %s: cancel did NOT take (attempt "
                              "%d, cancelled=%s, state=%s) — the order may "
                              "still be LIVE on the exchange; keeping it "
                              "tracked", comp_label(pe["comp"]),
                              pe["cancel_fails"], cancelled,
                              (st_ or ("unknown",))[0])
                if pe["cancel_fails"] == 3:
                    try:
                        notify("order_failed", component="fcfs-runner",
                               detail=(f"{pe['symbol']} entry limit "
                                       f"{pe['oid']} will not cancel and is "
                                       f"still live — CHECK THE ACCOUNT"))
                    except Exception:
                        pass
                return
        pending_entries.remove(pe)
        _pe_refund(pe)
        log.info("ENTRY LIMIT %s %s after %.0fs — %s", why,
                 comp_label(pe["comp"]), time.time() - pe["placed"],
                 "chasing with market" if LE_CHASE == "market" else "skipped")
        save(); tell_flat()
        if LE_CHASE != "market":
            return
        h = hosts.get(pe["group"])
        pxn = float(getattr(h, "last_px", 0) or pe["limit_px"])
        free = cascade_free()
        res, qty = ex_for(pe["symbol"]).open(pe["dir"], pe["lev"], pxn,
                                             margin_cap=free)
        if (res or {}).get("status") in ("success", "dry_run") and qty:
            fpx2 = float((res or {}).get("fill_price") or pxn)
            posn = _mk_posn(pe, qty, fpx2, chased=True)
            if free is not None:
                cs = (1.0 if mode == "spot"
                      else float((cfg.get("contract_sizes") or {})
                                 .get(pe["symbol"]) or 0))
                mg = qty * cs * fpx2 / pe["lev"]
                posn["margin"] = round(mg, 4)
                state["cascade_free"] = round(max(0.0, free - mg), 4)
            save(); tell_flat()
        else:
            log.warning("ENTRY LIMIT chase failed for %s: %s",
                        comp_label(pe["comp"]),
                        (res or {}).get("message"))

    # arbitration: same-bar ties resolved by component order (backtest rule)
    pending_opens = []   # [(bar_t, comp_i, dir, lev, px, group_key, src)]
    #                       src: "fresh" | "late_join" — recorded on the
    #                       position so the panel can mark shadow pickups
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
            if any(p.get("group") == key and p.get("comp") == ci
                   for p in positions):
                continue          # that IS a real position, not a shadow
            d = int(c.get("dir") or 1)
            lv = float(c.get("lev") or 1.0)
            pct = ((px / float(ep) - 1.0) * d * 100.0 *
                   (lv if mode == "lev" else 1.0)) if px else None
            rows.append(dict(comp=ci, label=comp_label(ci), group=key,
                             dir=d, lev=lv, entry_t=lbl,
                             entry_px=float(ep), px=px,
                             pct=round(pct, 3) if pct is not None else None,
                             exit_proj=c.get("exit_proj"),
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
    _adopt_backoff = [0.0]   # do not re-attempt adopts before this epoch

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
        Adopt button and the armed auto-adopt rules). FLAT-ONLY even under
        cascade — the sim validated fresh-signal cascading, not adoption."""
        if positions or pending_entries:
            log.warning("ADOPT (%s) refused: a position is already open "
                        "(or an entry limit is resting)", src)
            return False
        if paused():
            log.warning("ADOPT (%s) refused: instance is PAUSED", src)
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
        free = cascade_free()
        res, qty = ex_for(comps[ci]["pair"]).open(d, lv, px, margin_cap=free)
        if (res or {}).get("status") == "skipped":
            # deliberate no-trade (e.g. free USDT below the exchange minimum)
            # — NOT an error. Retrying every loop wrote 13k order_failed
            # notifications in 3 days (2026-09-01..04), ballooning
            # notifications.log past the origin-matcher's tail and breaking
            # the bot/manual P&L attribution. Log once, back off 10 min.
            if time.time() > _adopt_backoff[0]:
                log.warning("ADOPT (%s) skipped: %s — backing off 10 min",
                            src, (res or {}).get("message"))
            _adopt_backoff[0] = time.time() + 600
            return False
        if (res or {}).get("status") == "error" or not qty:
            notify("order_failed", account="fcfs", action="adopt",
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=(res or {}).get("message"))
            log.error("ADOPT open failed: %s", (res or {}).get("message"))
            return False
        fill = float((res or {}).get("fill_price") or px)
        state.setdefault("late_skips", {}).pop(str(ci), None)
        opened_bar[ci] = row["entry_t"]
        posn = dict(
            symbol=comps[ci]["pair"], comp=ci, dir=d, lev=lv, qty=qty,
            entry_price=fill, group=row["group"],
            mirror_entry_t=row["entry_t"], mirror_entry_px=ep, adopted=True,
            auto=(src == "auto"),
            armed_close_pct=(float(close_pct) if close_pct is not None
                             else None),
            exit_proj=row.get("exit_proj"),
            opened_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            opened_ms=int(time.time() * 1000))
        if free is not None:            # dry-run cascade paper accounting
            cs = (1.0 if mode == "spot"
                  else float((cfg.get("contract_sizes") or {})
                             .get(comps[ci]["pair"]) or 0))
            mg = qty * cs * fill / lv if mode == "lev" else qty * fill
            posn["margin"] = round(mg, 4)
            state["cascade_free"] = round(max(0.0, free - mg), 4)
        positions.append(posn)
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
        rtp_sync(posn)
        return True

    # ---- ARMED shadow rules: adopt automatically when a chosen shadow's
    # unrealized falls to <= adopt_pct (usually negative); optionally close
    # the adopted position when OUR unrealized reaches >= close_pct.
    # Rules live in a panel-written sidecar so they survive restarts; a rule
    # fires ONCE and is removed; rules whose virtual trade ended are pruned.
    ar_path = os.path.join(HERE, ".armed_" +
                           os.path.basename(cfg["state_file"]))
    _ar = {"m": 0.0, "rules": [], "auto": None}

    def _armed_load():
        try:
            m = os.path.getmtime(ar_path)
        except OSError:
            if _ar["rules"] or _ar["auto"]:
                _ar["rules"], _ar["auto"] = [], None
            return
        if m != _ar["m"]:
            _ar["m"] = m
            try:
                doc = json.load(open(ar_path))
                _ar["rules"] = doc.get("rules") or []
                _ar["auto"] = doc.get("auto") or None
                log.info("armed rules loaded: %d, auto=%s",
                         len(_ar["rules"]), _ar["auto"])
            except Exception:
                _ar["rules"], _ar["auto"] = [], None

    def _armed_write():
        try:
            if _ar["rules"] or _ar["auto"]:
                tmp = ar_path + ".tmp"
                json.dump(dict(rules=_ar["rules"], auto=_ar["auto"]),
                          open(tmp, "w"), indent=1)
                os.replace(tmp, ar_path)
            elif os.path.exists(ar_path):
                os.remove(ar_path)
            _ar["m"] = (os.path.getmtime(ar_path)
                        if os.path.exists(ar_path) else 0.0)
        except OSError:
            pass

    def check_auto():
        """Standing per-instance rule: whenever the slot is flat, adopt the
        DEEPEST-red shadow whose unrealized is <= adopt_pct. Not one-shot —
        stays active until disabled in the panel. Anti-churn guard: after a
        close of an auto-adopted position, the SAME virtual trade is only
        re-adopted once price has dropped >=1% below that exit."""
        au = _ar["auto"]
        if (not au or not au.get("enabled") or positions
                or time.time() < _adopt_backoff[0]):
            return
        thr = float(au.get("adopt_pct", -1e9))
        # optional pair restriction and PER-PAIR depths (research
        # 2026-08-25: thresholds scaled to each pair's volatility — deeper
        # for calm pairs — beat every single-threshold variant: hype -3,
        # sui -3.6, doge -4.5, sol -4.6, eth -5.0, xrp -5.3, btc -6.6)
        pair_pcts = {str(k).strip().lower().split("_")[0]: float(v)
                     for k, v in (au.get("pair_pcts") or {}).items()}
        pairs = {str(p).strip().lower().split("_")[0]
                 for p in (au.get("pairs") or []) if str(p).strip()}
        pairs |= set(pair_pcts)
        last = state.get("auto_last") or {}
        best = None
        for rows in shadow_by_key.values():
            for r in rows:
                pr = r["group"].split("_")[0].lower()
                if pairs and pr not in pairs:
                    continue
                pct = r.get("pct")
                if pct is None or pct > pair_pcts.get(pr, thr):
                    continue
                if (last and r["comp"] == last.get("comp")
                        and r["entry_t"] == last.get("entry_t")):
                    px = r.get("px")
                    xp = last.get("exit_px")
                    if not px or not xp or px > float(xp) * 0.99:
                        continue          # not 1% below our last exit yet
                # skip candidates do_adopt's liq-distance guard would refuse
                # anyway. Without this, one ZOMBIE shadow (a virtual trade
                # past its real liquidation point that the sim never killed —
                # e.g. the DOGE v7 short sitting at -151% on 2026-08-30)
                # is picked as "deepest" every cycle, gets refused, and
                # STARVES auto-adopt of every other eligible shadow.
                lv = float(r.get("lev") or 1.0) if mode == "lev" else 1.0
                if lv > 1:
                    adv = -pct / (100.0 * lv)     # adverse price fraction
                    if (adv / max(1.0 / lv - 0.008, 1e-9)
                            > float(cfg.get("late_join_max_drawdown", 0.5))):
                        continue
                if best is None or pct < best.get("pct"):
                    best = r
        if best is not None:
            log.info("AUTO-adopt trigger: %s at %+.2f%% <= %.2f%%",
                     comp_label(best["comp"]), best["pct"], thr)
            do_adopt(best["comp"], best["entry_t"],
                     close_pct=au.get("close_pct"), src="auto")

    def check_armed():
        _armed_load()
        if positions or paused():
            return    # paused: rules stay armed, nothing fires
        if not _ar["rules"]:
            check_auto()
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
        if not positions:
            check_auto()

    def check_armed_close():
        for pos2 in list(positions):
            cap = pos2.get("armed_close_pct")
            if cap is None:
                continue
            h = hosts.get(pos2.get("group"))
            px = float(getattr(h, "last_px", None) or 0)
            if not px:
                continue
            d = int(pos2.get("dir") or 1)
            lv = float(pos2.get("lev") or 1.0) if mode == "lev" else 1.0
            pct = (px / float(pos2["entry_price"]) - 1.0) * d * 100.0 * lv
            if pct >= float(cap):
                log.info("ARMED close: %+.2f%% >= %.2f%% target",
                         pct, float(cap))
                do_close(pos2, "armed_target", px)

    def flush_pending():
        nonlocal pending_opens
        if (not pending_opens or paused()
                or (not cascade and (positions or pending_entries))):
            pending_opens = []
            return
        pending_opens.sort(key=lambda x: (x[0], x[1]))   # (bar time, comp idx)
        todo = pending_opens if cascade else pending_opens[:1]
        pending_opens = []
        opened = None
        for bar_t, i, d, lev, px, gkey, osrc in todo:
            c = comps[i]
            if mode != "lev":
                lev = 1.0
            if sym_held(c["pair"]) or pos_of_comp(i) is not None:
                log.info("CASCADE skip %s: %s already held",
                         comp_label(i), c["pair"])
                continue
            if max_slots and len(positions) + len(pending_entries) >= max_slots:
                log.info("CASCADE stop: %d slots in use (max %d)",
                         len(positions) + len(pending_entries), max_slots)
                break
            free = cascade_free()
            if free is not None and free < float(
                    cfg.get("cascade_min_alloc", 20.0)):
                log.info("CASCADE stop: free capital %.2f below minimum "
                         "allocation — %s not opened", free, comp_label(i))
                break
            # `px` is the last CLOSED bar's close, and we fire up to 1.5s
            # later (arbitration window) plus poll latency. Size and anchor
            # on the LIVE tick instead: entry_price is the denominator of
            # the emergency-exit net, the liq-proximity warning and the
            # reported P&L.
            h = hosts.get(gkey)
            px_live = float(getattr(h, "last_px", None) or px)
            if le_on:
                # maker entry: rest a post-only limit; the pending-entries
                # loop promotes it to a position on fill (or times it out)
                lpx = px_live * (1 - LE_OFF if d > 0 else 1 + LE_OFF)
                res, qty = ex_for(c["pair"]).open(d, lev, px_live,
                                                  margin_cap=free,
                                                  entry_limit=dict(px=lpx))
                if (res or {}).get("status") == "resting" and qty:
                    pe = dict(symbol=c["pair"], comp=i, dir=d, lev=lev,
                              qty=qty,
                              limit_px=float((res or {}).get("limit_px")
                                             or lpx),
                              oid=(res or {}).get("order_id"), group=gkey,
                              bar_t=bar_t, src=osrc, placed=time.time())
                    if free is not None:
                        cs = float((cfg.get("contract_sizes") or {})
                                   .get(c["pair"]) or 0)
                        pe["margin"] = round(qty * cs * pe["limit_px"]
                                             / lev, 4)
                        state["cascade_free"] = round(
                            max(0.0, free - pe["margin"]), 4)
                    pending_entries.append(pe)
                    opened_bar[i] = bar_t
                    log.info("ENTRY LIMIT resting %s dir=%+d qty=%s @ %.6g "
                             "(signal bar %s, %s)", comp_label(i), d, qty,
                             pe["limit_px"], bar_t, osrc)
                    save(); tell_flat()
                    opened = bar_t
                    continue
                log.warning("ENTRY LIMIT placement failed for %s (%s) — "
                            "falling back to market", comp_label(i),
                            (res or {}).get("message"))
            res, qty = ex_for(c["pair"]).open(d, lev, px_live,
                                              margin_cap=free)
            if (res or {}).get("status") == "error":
                notify("order_failed", account="fcfs", action="open",
                       config=os.path.basename(cfg.get("_path", "?")),
                       detail=res.get("message"))
                continue
            if not qty or qty <= 0:
                continue
            # prefer the venue's own fill price when the executor confirmed it
            fpx = float((res or {}).get("fill_price") or px_live)
            posn = dict(
                symbol=c["pair"], comp=i, dir=d, lev=lev, qty=qty,
                entry_price=fpx, group=gkey,
                mirror_entry_t=None,   # bound post-flush / from the bar msg
                late_join=(osrc == "late_join"),
                opened_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                opened_ms=int(time.time() * 1000))
            if free is not None:        # dry-run cascade paper accounting
                cs = (1.0 if mode == "spot"
                      else float((cfg.get("contract_sizes") or {})
                                 .get(c["pair"]) or 0))
                mg = qty * cs * fpx / lev if mode == "lev" else qty * fpx
                posn["margin"] = round(mg, 4)
                state["cascade_free"] = round(max(0.0, free - mg), 4)
            positions.append(posn)
            log.info("OPEN %s dir=%+d lev=%.1f qty=%s px=%.6g "
                     "(signal bar %s, %s%s)",
                     comp_label(i), d, lev, qty, fpx, bar_t, osrc,
                     (f", slot {len(positions)}" if cascade else ""))
            notify("position_opened", account="fcfs",
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=c["pair"], side=("LONG" if d > 0 else "SHORT"),
                   qty=qty, lev=lev, price=fpx, comp=comp_label(i),
                   live=(not cfg["dry_run"]))
            save(); tell_flat()
            opened = bar_t
        return opened

    opened_bar = {}   # comp_i -> mirror entry_t recorded at open time

    tell_flat()
    last_note = 0
    # heartbeat: the loop is silent unless something happens, so a stalled
    # feed looks exactly like a quiet market. Every HB_EVERY seconds log what
    # each group last delivered, and flag any group whose bars stopped
    # arriving (> 4 bar intervals) — that is the actionable failure.
    HB_EVERY = 900
    last_hb = time.time()
    _silent_said = {}          # group -> last time we warned it was silent
    last_bar_at = {}      # group key -> (bar label, epoch received)
    bars_seen = [0]
    while True:
        try:
            try:
                key, msg = q.get(timeout=1.0)
            except queue.Empty:
                key, msg = None, None

            now = time.time()

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
                    if (not positions or cascade) and not paused():
                        sk = state.setdefault("late_skips", {})
                        for ci, c in sorted(cbyi.items()):
                            if c.get("opens_now"):
                                opened_bar[ci] = c.get("open")
                                pending_opens.append(
                                    (bt, ci, int(c.get("dir") or 1),
                                     float(c.get("lev") or 1.0),
                                     float(msg.get("px") or 0), key,
                                     "fresh"))
                                continue
                            if positions:
                                # late-join stays FLAT-ONLY under cascade —
                                # the sim validated fresh-signal cascading
                                continue
                            # With auto-adopt enabled, mid-trade joins are
                            # governed ONLY by its per-pair thresholds: the
                            # adopt-sim (2026-08-25) showed the backtest
                            # baseline never joins already-open virtual
                            # trades, and unthresholded joins underperform
                            # fresh-signals-only. Late-join below stays as
                            # the legacy behavior when auto-adopt is OFF.
                            if (_ar["auto"] or {}).get("enabled"):
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
                                     px, key, "late_join"))
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
                    # follow the mirror of EVERY position owned by this group
                    for pos in [p for p in list(positions)
                                if p.get("group") == key
                                and p.get("comp") in cbyi]:
                            me = cbyi[pos["comp"]]
                            # engine's projected close for OUR position —
                            # refreshed every bar; persisted by the regular
                            # shadow/save cadence
                            if me.get("exit_proj") is not None:
                                pos["exit_proj"] = me.get("exit_proj")
                                rtp_sync(pos)
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
                                    do_close(pos, "virtual_exit_fast",
                                             msg.get("px"))
                                else:
                                    pos["mirror_entry_t"] = bind
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
                                    do_close(pos, "virtual_exit",
                                             msg.get("px"))

            # panel-requested adopt of a chosen shadow (only when flat) +
            # armed auto-adopt / auto-close rules
            if not positions and not pending_entries:
                try_adopt()
                check_armed()
            elif positions:
                check_armed_close()

            # arbitration window expired?
            if pending_deadline and now >= pending_deadline:
                pending_deadline = 0.0
                if flush_pending():
                    _bchg = False
                    for p in positions:
                        if (p.get("mirror_entry_t") is None
                                and opened_bar.get(p["comp"]) is not None):
                            p["mirror_entry_t"] = opened_bar.get(p["comp"])
                            _bchg = True
                    if _bchg:
                        save()

            # resting entry limits: promote fills to positions, time out
            # stale ones (cancel -> chase or skip). Dry-run paper-fills the
            # moment the live price crosses the limit.
            for pe in list(pending_entries):
                _h = hosts.get(pe["group"])
                _pxn = float(getattr(_h, "last_px", 0) or 0)
                if pe["oid"] == "dry":
                    if _pxn and ((_pxn <= pe["limit_px"]) if pe["dir"] > 0
                                 else (_pxn >= pe["limit_px"])):
                        _entry_filled(pe, pe["qty"], pe["limit_px"])
                        continue
                elif now - float(pe.get("checked", 0)) > 10:
                    pe["checked"] = now
                    st_ = ex_for(pe["symbol"]).entry_state(pe["oid"],
                                                           pe["dir"])
                    if st_ and st_[0] == "filled":
                        _entry_filled(pe, st_[1], st_[2])
                        continue
                    if st_ and st_[0] == "gone":
                        # post-only got cancelled by the exchange (it would
                        # have crossed) or someone pulled it — policy applies
                        _entry_timeout(pe, "order gone")
                        continue
                if now - float(pe["placed"]) > LE_TIMEOUT:
                    _entry_timeout(pe, "timed out")

            # protective intra-bar checks on the live price of each open pair
            if positions:
                # manual override sidecar (one armed rule per instance;
                # pos_key picks the position) — read ONCE per round
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
            for pos in list(positions):
                h = hosts.get(pos["group"])
                px = h.last_px if h else None
                if not px:
                    continue
                # ---- resting TP bookkeeping (fills happen exchange-side) --
                if rtp_on and pos.get("tp_order_id") and pos.get("tp_px"):
                    _tp = float(pos["tp_px"])
                    _hit = (px >= _tp) if pos.get("dir", 1) > 0 else (px <= _tp)
                    if pos["tp_order_id"] == "dry":
                        if _hit:      # paper fill at the exact target price
                            log.info("RESTING TP paper-fill %s @ %.6g",
                                     comp_label(pos["comp"]), _tp)
                            do_close(pos, "resting_tp_fill", _tp)
                            continue
                    elif _hit or now - float(pos.get("tp_checked", 0)) > 45:
                        pos["tp_checked"] = now
                        st_ = ex_for(pos["symbol"]).tp_state(pos["tp_order_id"])
                        if st_ == "filled":
                            log.info("RESTING TP FILLED %s @ %.6g (maker)",
                                     comp_label(pos["comp"]), _tp)
                            pos["tp_order_id"] = None
                            do_close(pos, "resting_tp_fill", _tp)
                            continue
                        if st_ == "gone":
                            log.warning("resting TP order vanished but %s is "
                                        "still held — re-placing on next bar",
                                        pos["symbol"])
                            pos["tp_order_id"] = None
                            pos.pop("tp_px", None)
                            save()
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
                    do_close(pos, "emergency_exit", px)
                    continue
                # ---- STANDALONE position (its sim liquidated) ----
                # It has no mirror left to follow, so it gets an explicit
                # plan anchored to OUR entry: take the component's own
                # edge if it arrives. On the DOWNSIDE, leveraged positions
                # are NEVER closed by this guard — Adrian's rule
                # (2026-09-10): only a strategy's own stop-loss may realize
                # a leveraged loss; liquidation is the exchange's call.
                # (The old 50%-of-liq-distance stop realized -$1,180 on the
                # Sep 6 HYPE spike top, which never reached MEXC's actual
                # liquidation price and mean-reverted within minutes.)
                # Spot keeps its -50% disaster brake: no exchange
                # liquidation exists there to backstop it.
                elif pos.get("standalone"):
                    tp = float(cfg.get("standalone_take_profit", 0.005))
                    if adverse >= tp:
                        log.info("STANDALONE take-profit hit (+%.2f%% "
                                 "from our entry)", 100 * adverse)
                        do_close(pos, "standalone_take_profit", px)
                        continue
                    elif mode != "lev":
                        sl_frac = float(cfg.get("standalone_stop_frac", 0.5))
                        if adverse <= -abs(sl_frac):
                            log.warning("STANDALONE stop hit (%.2f%% "
                                        "adverse, spot disaster brake at "
                                        "%.0f%%)",
                                        100 * -adverse, 100 * sl_frac)
                            do_close(pos, "standalone_stop", px)
                            continue
                # manual override: panel-set trigger for THIS position
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
                    do_close(pos, "manual_override", px)

            # due host restarts (scheduled, never blocking — see "died")
            for _k, _h in hosts.items():
                if _h.restart_at and now >= _h.restart_at and not _h.alive():
                    _h.restart_at = 0.0
                    log.info("restarting host %s now", _k)
                    _h.start()
                    tell_flat()

            # watchdog: a silent host while we hold ITS position is a hazard
            for _g in {p.get("group") for p in positions}:
                h = hosts.get(_g)
                if not (h and h.alive()):
                    continue
                # last_seen starts at 0.0, so a host that has not produced its
                # first bar yet reads as ~1.8e9 seconds silent and this fired
                # EVERY loop through the ~60s backfill — 11 warnings in 9s on
                # a restart, drowning the real thing. Age from START until the
                # first bar arrives, and log at most once a minute.
                _since = h.last_seen or h.started_at
                if not _since or time.time() - _since <= 300:
                    continue
                if time.time() - _silent_said.get(_g, 0) < 60:
                    continue
                _silent_said[_g] = time.time()
                log.warning("host %s silent >%.0fmin while positioned", _g,
                            (time.time() - _since) / 60)

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
                where = (", ".join(
                    f"{p['symbol']} via {comp_label(p['comp'])}"
                    for p in positions) if positions else "empty")
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
