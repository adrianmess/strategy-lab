#!/usr/bin/env python3
"""Adaptive live trader.

Main loop:
  - maintain 3m/1m kline history from MEXC public API
  - on each new closed 3m bar: evaluate the adaptive strategy
  - execute via the existing Playwright webhook server (webhook_server.py)
  - protective stop also enforced intra-bar on every poll

Run:  python3 trader.py            (uses config.json, starts in dry_run)
      python3 trader.py --live     (POSTs to the webhook server)
"""
import argparse
import json
import logging
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from data_feed import Feed              # noqa: E402
from notify import notify               # noqa: E402


def _acct_label(cfg):
    exe = cfg.get("execution") == "api" and "api" or "browser"
    acct = cfg.get("api_account") or "mexc1"
    return f"{acct}/{exe}"

CFG_PATH = os.path.join(HERE, "config.json")


def make_strategy(cfg, state):
    """Route by candidate format: V7 (engine3 full-param, 'regs' list),
    V6 (wf2 format), or legacy params dict."""
    cand = cfg.get("candidate")
    if cand and cand.get("strategy") == "metax":
        from strategy_metax import StrategyMetax
        return StrategyMetax(cfg, state)
    if cand and (cand.get("strategy") == "v7" or "regs" in cand):
        from strategy_v7 import StrategyV7
        return StrategyV7(cfg, state)
    if cand:
        from strategy_v6 import StrategyV6
        return StrategyV6(cfg, state)
    from strategy import Strategy
    return Strategy(cfg, state)


def setup_logging(cfg):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(HERE, cfg["log_file"]))],
    )


def load_state(cfg):
    path = os.path.join(HERE, cfg["state_file"])
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(cfg, state):
    path = os.path.join(HERE, cfg["state_file"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=float)
    os.replace(tmp, path)


class Executor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("executor")

    def _post(self, payload):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would POST: %s", json.dumps(payload))
            return {"status": "dry_run"}
        r = requests.post(self.cfg["webhook_url"], json=payload, timeout=120)
        r.raise_for_status()
        out = r.json()
        self.log.info("webhook response: %s", out)
        return out

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        notional = cfg["equity_usdt"] * lev
        qty = int(notional / price / cfg["contract_size"])
        if qty < 1:
            self.log.warning("qty < 1 contract, skipping (equity too small)")
            return None, 0
        action = "open_long" if direction > 0 else "open_short"
        res = self._post({"action": action, "symbol": cfg["symbol"],
                          # FLOOR, never round up: a fractional-leverage config
                          # (pre-integer-search) must not trade at MORE leverage
                          # than it was backtested with
                          "leverage": max(1, int(lev)), "quantity": qty})
        return res, qty

    def close_position(self):
        return self._post({"action": "close_position", "symbol": self.cfg["symbol"]})


class APIExecutor:
    """Native MEXC futures API execution (config: "execution": "api").
    Same interface as Executor; dry_run only logs, exactly like the webhook
    path. NOTE: futures only — MEXC spot API trading is still restricted to
    selected BTC/ETH pairs, so spot instances keep the browser executor."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("api-executor")
        if cfg.get("mode") == "spot":
            raise SystemExit('execution:"api" is futures-only — MEXC spot API '
                             "trading is restricted (BTC/ETH pairs only). "
                             "Use the browser executor for spot.")
        from mexc_api import MexcFuturesAPI
        self.api = MexcFuturesAPI(account=cfg.get("api_account"))
        self.log.info("MEXC futures API executor ready (account=%s, proxy=%s)",
                      self.api.account, bool(self.api.proxies))

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        # LIVE sizing = 100% of what the account actually has (the backtests
        # compound full equity every trade — a fixed equity_usdt either
        # bounces orders when the account is smaller, e.g. code 2005
        # "Balance insufficient", or undertrades when it has grown).
        # equity_usdt remains the DRY-RUN paper size and the fallback.
        margin = float(cfg["equity_usdt"])
        if not cfg["dry_run"]:
            try:
                u = [a for a in self.api.assets()
                     if a.get("currency") == "USDT"]
                avail = float((u[0].get("availableOpen")
                               or u[0].get("availableBalance") or 0)) if u else 0.0
                if avail > 0:
                    margin = avail * 0.98   # fee/rounding headroom
                    self.log.info("sizing from live balance: %.2f USDT "
                                  "available -> margin %.2f", avail, margin)
            except Exception as e:
                self.log.warning("balance query failed (%s) — falling back "
                                 "to equity_usdt=%.2f", e, margin)
        notional = margin * lev
        qty = int(notional / price / cfg["contract_size"])
        if qty < 1:
            self.log.warning("qty < 1 contract, skipping (equity too small)")
            return None, 0
        lev_i = max(1, int(lev))   # FLOOR — never more leverage than backtested
        if cfg["dry_run"]:
            self.log.info("[DRY RUN] would API-%s %d contracts at ~%.3f lev %d",
                          "LONG" if direction > 0 else "SHORT", qty, price, lev_i)
            return {"status": "dry_run"}, qty
        try:
            fn = self.api.open_long if direction > 0 else self.api.open_short
            res = fn(cfg["symbol"], qty, lev_i, price)
            self.log.info("API order placed: %s", res)
            return {"status": "success", "order": res}, qty
        except Exception as e:
            self.log.error("API order FAILED: %s", e)
            return {"status": "error", "message": str(e)}, 0

    def close_position(self):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would API-close all %s positions",
                          self.cfg["symbol"])
            return {"status": "dry_run"}
        try:
            res = self.api.close_position(self.cfg["symbol"])
            self.log.info("API close: %s", res)
            return {"status": "success", "result": res}
        except Exception as e:
            self.log.error("API close FAILED: %s", e)
            return {"status": "error", "message": str(e)}


class APISpotExecutor:
    """Native MEXC SPOT API execution (config: "execution": "api" + mode spot).
    Market BUY spends equity_usdt of USDT; market SELL closes the tracked
    quantity (read from the state file, falling back to the free SOL balance,
    capped so personal holdings on the account are never touched beyond it)."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("api-spot-executor")
        from mexc_api import MexcSpotAPI
        self.api = MexcSpotAPI(account=cfg.get("api_account"))
        base = cfg["symbol"].split("_")[0]
        self.base_asset = base
        self.log.info("MEXC SPOT API executor ready (account=%s, proxy=%s)",
                      self.api.account, bool(self.api.proxies))

    def open_position(self, direction, lev, price):
        cfg = self.cfg
        if direction < 0:
            self.log.error("spot cannot short — signal ignored")
            return {"status": "error", "message": "spot cannot short"}, 0
        # LIVE sizing = 100% of free USDT on the account (see APIExecutor);
        # equity_usdt is the dry-run paper size and the fallback
        quote = float(cfg["equity_usdt"])       # spot is always 1x
        if not cfg["dry_run"]:
            try:
                free = float(self.api.balance("USDT") or 0)
                if free > 0:
                    quote = free * 0.999        # rounding headroom
                    self.log.info("sizing from live balance: %.2f USDT free "
                                  "-> spending %.2f", free, quote)
            except Exception as e:
                self.log.warning("balance query failed (%s) — falling back "
                                 "to equity_usdt=%.2f", e, quote)
        qty_est = quote / price
        if cfg["dry_run"]:
            self.log.info("[DRY RUN] would SPOT-BUY %.2f USDT (~%.4f %s) at ~%.3f",
                          quote, qty_est, self.base_asset, price)
            return {"status": "dry_run"}, qty_est
        try:
            res = self.api.market_buy_quote(cfg["symbol"], quote)
            qty = float(res.get("executedQty") or 0) or qty_est
            self.log.info("SPOT BUY filled: qty=%.4f order=%s", qty,
                          res.get("orderId"))
            return {"status": "success", "order": res}, qty
        except Exception as e:
            self.log.error("SPOT BUY FAILED: %s", e)
            return {"status": "error", "message": str(e)}, 0

    def _tracked_qty(self):
        try:
            st = json.load(open(os.path.join(HERE, self.cfg["state_file"])))
            pos = st.get("position") or {}
            return float(pos.get("qty") or 0)
        except Exception:
            return 0.0

    def close_position(self):
        if self.cfg["dry_run"]:
            self.log.info("[DRY RUN] would SPOT-SELL tracked %s position",
                          self.base_asset)
            return {"status": "dry_run"}
        try:
            qty = self._tracked_qty()
            free = self.api.balance(self.base_asset)
            sell = min(qty, free) if qty > 0 else free
            if sell <= 0:
                self.log.warning("nothing to sell (tracked=%.4f free=%.4f)",
                                 qty, free)
                return {"status": "success", "note": "nothing to sell"}
            res = self.api.market_sell(self.cfg["symbol"], sell)
            self.log.info("SPOT SELL filled: qty=%.4f order=%s", sell,
                          res.get("orderId"))
            return {"status": "success", "order": res}
        except Exception as e:
            self.log.error("SPOT SELL FAILED: %s", e)
            return {"status": "error", "message": str(e)}


_GUARD_FH = None      # module-level: the flock lives as long as the process


def _single_instance_guard(cfg):
    """Refuse to start if another trader already owns this config's STATE
    FILE. Two traders sharing state corrupt each other's position tracking
    (and on a live config would double orders). The panel checks this too,
    but it forgets running processes across its own restarts — this lock is
    held by the kernel, so it is authoritative. (Observed 2026-08-12: a
    panel restart orphaned a trader; a second one then ran the same dry-run
    config for 4 hours, double-polling MEXC and clobbering the state file.)"""
    global _GUARD_FH
    import fcntl
    sf = os.path.basename(cfg.get("state_file", "trader_state.json"))
    path = os.path.join(HERE, f".{sf}.lock")
    # r+ (not w) — 'w' truncates before we know whether we own the lock,
    # which would erase the holder's pid from the message
    fh = open(path, "r+" if os.path.exists(path) else "w+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            other = open(path).read().strip() or "unknown"
        except Exception:
            other = "unknown"
        raise SystemExit(
            f"REFUSING TO START: another trader (pid {other}) is already "
            f"running with state file '{sf}'. Stop it first "
            f"(panel Instances, or `kill {other}`).")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _GUARD_FH = fh        # keep the handle alive → keep the lock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="disable dry_run")
    ap.add_argument("--config", default=os.environ.get("TRADER_CONFIG", "config.json"),
                    help="config file name (in this directory) or absolute path")
    args = ap.parse_args()

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(HERE, args.config)
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["_path"] = cfg_path   # router strategies hot-reload re-assignments
    _single_instance_guard(cfg)
    # FCFS combos (multi-pair, multi-timeframe) run their own loop — delegate
    # so the panel's instance machinery / --live / dry-run all work unchanged
    if (cfg.get("candidate") or {}).get("strategy") == "fcfsx":
        from fcfs_runner import main_fcfs
        main_fcfs(cfg, args.live)
        return
    if args.live:
        cfg["dry_run"] = False
    # chart timeframe: MUST be in the environment BEFORE any research module
    # is imported (make_strategy) — regimes.DAY and engine bar/hour
    # conversions read LAB_TF at import/call time
    tf_min = int(str(cfg.get("timeframe") or "3m").rstrip("m"))
    if tf_min not in (1, 3, 5):
        raise SystemExit(f"unsupported timeframe {cfg.get('timeframe')!r} "
                         f"(supported: 1m, 3m, 5m)")
    os.environ["LAB_TF"] = str(tf_min)
    setup_logging(cfg)
    log = logging.getLogger("trader")
    log.info("starting (dry_run=%s, timeframe=%dm)", cfg["dry_run"], tf_min)

    state = load_state(cfg)
    strat = make_strategy(cfg, state)
    if cfg.get("execution") == "api":
        ex = APISpotExecutor(cfg) if cfg.get("mode") == "spot" else APIExecutor(cfg)
        log.info("execution path: MEXC %s API",
                 "SPOT" if cfg.get("mode") == "spot" else "futures")
    else:
        ex = Executor(cfg)
        log.info("execution path: browser webhook")

    is_router = (cfg.get("candidate") or {}).get("strategy") == "metax"
    feed = Feed(cfg["symbol"], anchored=is_router, tf_min=tf_min)
    feed.backfill()
    last_closed = None

    check = getattr(strat, "intrabar_check", None) or strat.intrabar_stop

    # ---------------- late-join (single-strategy families) ----------------
    # If the SIM would already be in a position (it opened while this trader
    # was down or being restarted), join it late — but ONLY in the red: our
    # entry at/better-than the sim's can only outperform the sim, while a
    # GREEN lev entry anchors liquidation at OUR worse price and can die on
    # a drawdown the sim survives. Each virtual trade is judged ONCE; green
    # skips persist in the state file, the virtual close is ignored, and
    # fresh signals resume afterward. Joined positions mirror the sim's exit
    # (engine replay), with the emergency intrabar net still active.
    # The metax router does this itself (strategy_metax); fcfsx likewise.
    _cand = cfg.get("candidate") or {}
    _fam = _cand.get("strategy") or ("v7" if "regs" in _cand else None)
    lj = {"on": _fam not in (None, "metax", "metax2", "pairx", "fcfsx")}

    def lj_replay(closed, d1):
        import strategy_metax as mx
        comp = dict(i=0, strategy=_fam, method=cfg.get("method", "vol3"),
                    cand=_cand)
        feats = mx.compute_features(closed)
        tr, op = mx.run_component_engine(comp, cfg["mode"], closed, d1, feats)
        lbl = mx._ts16(op["entry_t"]) if op is not None else None
        return op, lbl

    def lj_on_bar(closed, d1, price):
        """Startup / skip-mode / mirror-mode replay step. Returns True when
        the strategy's fresh OPEN actions must be suppressed this bar (a
        green-skipped virtual position is still open in the sim)."""
        if not lj["on"]:
            return False
        try:
            op, lbl = lj_replay(closed, d1)
        except Exception as e:
            log.info("late-join disabled (replay unavailable for '%s': %s)",
                     _fam, e)
            lj["on"] = False
            return False
        pos = state.get("position")
        # A) holding a late-joined position: the replay is the exit authority
        if pos and pos.get("late_mirror"):
            if lbl != pos["late_mirror"]:
                log.info("LATE-JOIN CLOSE: virtual trade %s ended",
                         pos["late_mirror"])
                res = ex.close_position()
                notify("position_closed", account=_acct_label(cfg),
                       config=os.path.basename(cfg.get("_path", "?")),
                       symbol=cfg["symbol"], reason="virtual_exit(late-join)",
                       price=price, live=(not cfg["dry_run"]),
                       position=pos, result=(res or {}).get("status"))
                state["position"] = None
                save_state(cfg, state)
            return False
        if pos:
            return False
        # B) flat
        if op is None:
            if state.get("late_skip"):
                state["late_skip"] = None
                save_state(cfg, state)
                log.info("late-join: skipped virtual trade closed — fresh "
                         "signals resume")
            return False
        ei = int(op.get("entry_idx", -1))
        if not (0 <= ei < len(closed)) or ei == len(closed) - 1:
            return False              # fresh open — normal path handles it
        if state.get("late_skip") == lbl:
            return True               # judged green earlier — keep waiting
        ep = float(closed["close"].iloc[ei])
        d = 1 if float(op.get("dir", 1)) >= 0 else -1
        red = (price < ep) if d > 0 else (price > ep)
        # red-only is the LEVERAGED exception (a green lev entry anchors
        # liquidation at our worse price); spot cannot liquidate, so a spot
        # instance joins its virtual position regardless of color
        if cfg["mode"] != "spot" and not red:
            state["late_skip"] = lbl
            save_state(cfg, state)
            log.info("LATE-JOIN skipped: virtual pos %s @%.6g is GREEN at "
                     "%.6g (lev) — waiting for the next fresh signal",
                     lbl, ep, price)
            return True
        lev = float(op.get("lev", 1.0)) if cfg["mode"] == "lev" else 1.0
        res, qty = ex.open_position(d, lev, price)
        if (res or {}).get("status") == "error":
            notify("order_failed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   action="open", detail=res.get("message"))
            return False
        if qty and qty > 0:
            state["late_skip"] = None
            state["position"] = dict(
                dir=d, system=0, regime=0, entry_price=ep, qty=qty, lev=lev,
                sl_price=0.0,
                entry_sig_ms=float(closed["t"].iloc[ei].value // 10**6),
                opened_at=str(closed["t"].iloc[-1]), fill_price=price,
                late_mirror=lbl)
            log.info("LATE-JOIN OPEN dir=%+d lev=%.1f qty=%s: virtual entry "
                     "%s @%.6g, filled %.6g (%s)", d, lev, qty, lbl, ep, price,
                     "red" if red else "green/spot")
            notify("position_opened", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"],
                   side=("LONG" if d > 0 else "SHORT"), qty=qty, lev=lev,
                   price=price, live=(not cfg["dry_run"]),
                   note=f"late-join in the red (virtual entry {ep:g})")
            save_state(cfg, state)
        return False

    def protective_check():
        """Run the intra-bar protective/emergency stop against the LIVE price.
        Returns True if it closed the position. Single-threaded (called only
        from the main loop) so it can never race the bar-close order logic."""
        if not state.get("position"):
            return False
        price = feed.last_price()
        act = check(price)
        if act:
            log.warning("INTRABAR STOP at %.3f (%s price): %s",
                        price, feed.price_source(), act)
            res = ex.close_position()
            notify("position_closed", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   symbol=cfg["symbol"], reason=act.get("reason"),
                   price=price, live=(not cfg["dry_run"]),
                   result=(res or {}).get("status"))
            state["position"] = None
            save_state(cfg, state)
            return True
        return False

    # How often to re-check the protective stop against live ticks, in seconds.
    # Defaults to 0.5s (near-live); heavy work (kline fetch + bar-close eval)
    # still runs once per poll_seconds. Set protect_poll_seconds <= 0 to restore
    # the old single-check-per-poll behavior.
    protect_dt = cfg.get("protect_poll_seconds", 0.5)

    while True:
        try:
            # anchored feed may only re-anchor its window while we're flat
            feed.trim_ok = (not state.get("position")
                            and not state.get("mirror"))
            feed.update()
            price = feed.last_price()

            # 1) intra-bar protective check (live price)
            protective_check()

            # 2) new closed bar?
            closed = feed.closed_bars()
            newest = closed["t"].iloc[-1] if len(closed) else None
            if newest is not None and newest != last_closed:
                first_eval = last_closed is None
                last_closed = newest
                lo = closed["t"].iloc[0]
                d1 = feed.df1[feed.df1["t"] >= lo].reset_index(drop=True)
                # late-join replay runs only when it can matter: the first
                # bar after start, while a green skip is pending, or while
                # holding a late-joined (replay-mirrored) position
                lj_suppress = False
                if lj["on"] and (first_eval or state.get("late_skip")
                                 or (state.get("position") or {})
                                 .get("late_mirror")):
                    lj_suppress = lj_on_bar(closed, d1, price)
                actions = strat.on_bar_close(closed, d1)
                if (state.get("position") or {}).get("late_mirror"):
                    # replay is the exit authority for a late-joined position
                    actions = [a for a in actions if a["do"] != "close"]
                if lj_suppress:
                    actions = [a for a in actions if a["do"] != "open"]
                for a in actions:
                    if a["do"] == "close" and state.get("position"):
                        log.info("CLOSE (%s) pos=%s", a["reason"], state["position"])
                        res = ex.close_position()
                        notify("position_closed", account=_acct_label(cfg),
                               config=os.path.basename(cfg.get("_path", "?")),
                               symbol=cfg["symbol"], reason=a["reason"],
                               price=price, live=(not cfg["dry_run"]),
                               position=state.get("position"),
                               result=(res or {}).get("status"))
                        if (res or {}).get("status") == "error":
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="close", detail=res.get("message"))
                        state["position"] = None
                    elif a["do"] == "open" and not state.get("position"):
                        res, qty = ex.open_position(a["dir"], a["lev"], price)
                        if (res or {}).get("status") == "error":
                            notify("order_failed", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   action="open", detail=res.get("message"))
                        if qty > 0:
                            notify("position_opened", account=_acct_label(cfg),
                                   config=os.path.basename(cfg.get("_path", "?")),
                                   symbol=cfg["symbol"],
                                   side=("LONG" if a["dir"] > 0 else "SHORT"),
                                   qty=qty, lev=a["lev"], price=price,
                                   live=(not cfg["dry_run"]))
                            state["position"] = dict(
                                dir=a["dir"], system=a["system"], regime=a["regime"],
                                entry_price=price, qty=qty, lev=a["lev"],
                                sl_price=a["sl_price"], entry_sig_ms=a["sig_ms"],
                                opened_at=str(newest))
                            log.info("OPEN %s %s lev=%.2f qty=%d sl=%.3f regime=%d",
                                     "LONG" if a["dir"] > 0 else "SHORT", a["system"],
                                     a["lev"], qty, a["sl_price"], a["regime"])
                save_state(cfg, state)

            # 3) fast protective sub-loop: keep watching the LIVE price between
            # heavy polls so an adverse move is caught within ~protect_dt, not
            # after the full poll interval.
            if protect_dt and protect_dt > 0:
                t_end = time.time() + cfg["poll_seconds"]
                while time.time() < t_end:
                    time.sleep(min(protect_dt, max(0.0, t_end - time.time())))
                    if protective_check():
                        break
            else:
                time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.exception("loop error: %s", e)
            notify("trader_error", account=_acct_label(cfg),
                   config=os.path.basename(cfg.get("_path", "?")),
                   detail=str(e)[:300])
            time.sleep(30)


if __name__ == "__main__":
    main()
