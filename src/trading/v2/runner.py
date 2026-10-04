"""The V2 runner: public market data -> features -> state -> Jev -> policy -> risk -> PAPER book, on a clock.

PAPER ONLY. It reads public market data, calls Jev (if a key is in the environment), and fills a simulated book. It imports
no order client and has no path to one. Nothing here touches LIVE_ENABLED, cron, pm2 or any live script.

Run it:  PYTHONPATH=src python -m trading.v2.runner --demo --symbols BTCUSDT --cadence 15 --out data/local/v2
It does NOT start itself and is not deployed: a resident process on the VPS needs the owner's approval (RAM, supervision).

What a human can do while it runs:
  * create the file <out>/STOP        -> it exits cleanly after the current step
  * create the file <out>/V2_KILL     -> the kill switch reads as engaged (new entries blocked, resting paper orders cancelled)
  * read <out>/heartbeat.json         -> last step time, counts, outcomes, Jev share, paper equity, feed health

Safety defaults: no limits are defaulted (the caller supplies RiskLimits, KillLimits, ExitParams and the daily-loss budget);
`demo_config()` provides clearly labelled UNVALIDATED demo values. Without TYPESAFE_API_KEY every cycle falls back and says so.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from trading.replay.broker import CostModel
from trading.shadow.chain import ShadowChain
from trading.v2.adapter import JevAdapter, JevClient
from trading.v2.binance_public import BinancePublic, FeedError, LiveBook
from trading.v2.context import PaperContextProvider
from trading.v2.contracts import StrategyRef
from trading.v2.fallback import GatedSource
from trading.v2.features import N_1H, N_1M
from trading.v2.killswitch import KillController, KillLimits, Observation
from trading.v2.paper import PaperBook
from trading.v2.policy import ExitParams, PolicyConfig
from trading.v2.risk import ExchangeHealth, RiskLimits
from trading.v2.shadow import ShadowPipeline
from trading.v2.stances import ReplayStanceSource
from trading.v2.typesafe_client import TypeSafeJevClient
from trading.v2.workflow import WORKFLOW_DIR, JevWorkflow, load_workflow

log = logging.getLogger("trading.v2.runner")
MIN_MS = 60_000
HOUR_MS = 3_600_000
PRIME_MARGIN = 20


@dataclass(frozen=True)
class RunnerConfig:
    symbols: tuple[str, ...]
    cadence_minutes: int
    paper_equity: float
    out_dir: Path
    risk_limits: RiskLimits
    kill_limits: KillLimits
    exit_params: Optional[ExitParams]
    daily_loss_budget_usd: Optional[float]
    strategy: StrategyRef = StrategyRef(name="jev_v2", version="0.1")
    costs: CostModel = CostModel(fee_rate=0.001, spread_bps=0.0, slippage_bps=0.0)
    max_stale_ms: int = 20_000
    grace_s: float = 3.0
    jev_timeout_s: float = 8.0
    book_lead_tolerance_ms: int = 5_000     # a live book is fetched AFTER `now` was taken: allow the fetch latency
    stance_symbols: Optional[tuple[str, ...]] = None      # default: the traded symbols


def demo_config(symbols: tuple[str, ...], cadence_minutes: int, out_dir: Path) -> RunnerConfig:
    """UNVALIDATED DEMO VALUES. They exist so the runner can be exercised; the real numbers are the owner's (E7, E10)."""
    return RunnerConfig(
        symbols=symbols, cadence_minutes=cadence_minutes, paper_equity=1_000.0, out_dir=out_dir,
        risk_limits=RiskLimits(daily_loss_cap_usd=100.0),
        kill_limits=KillLimits(exchange_failures_to_engage=3, max_feed_age_ms=180_000, max_orders_per_minute=5,
                               max_abs_slippage_bps=50.0, max_price_move_frac=0.10, daily_loss_cap_usd=100.0,
                               min_request_confidence=0.8),
        exit_params=ExitParams(stop_pct=0.02, target_pct=0.04), daily_loss_budget_usd=50.0)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class Runner:
    def __init__(self, cfg: RunnerConfig, feed: BinancePublic, workflow: JevWorkflow, *,
                 jev_client: Optional[JevClient] = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.feed, self.workflow, self._clock, self._sleep = cfg, feed, workflow, clock, sleep
        out = cfg.out_dir
        out.mkdir(parents=True, exist_ok=True)
        self.start_ms = int(clock() * 1000)
        self.stances = ReplayStanceSource()
        self.book = PaperBook(cfg.paper_equity, cfg.costs, ShadowChain(out, "v2paper", "book", self.start_ms))
        self.ctl = KillController(cfg.kill_limits, ShadowChain(out, "v2kill", "controller", self.start_ms))
        self.provider = PaperContextProvider(
            self.book, cfg.strategy, cfg.risk_limits, exit_params=cfg.exit_params,
            daily_loss_budget_usd=cfg.daily_loss_budget_usd, exchange_health=self._health, kill_switch=self._kill,
            legacy_action=self._legacy, paused=lambda: self.ctl.paused_scopes)
        self.client = jev_client or TypeSafeJevClient(workflow)
        self.adapter = JevAdapter(self.client, workflow, GatedSource(self.stances), timeout_s=cfg.jev_timeout_s)
        self.pipeline = ShadowPipeline(
            adapter=self.adapter, provider=self.provider, sink=ShadowChain(out, "v2cycle", "pipeline", self.start_ms),
            book=LiveBook(feed), policy=PolicyConfig(), limits=cfg.risk_limits, cadence_minutes=cfg.cadence_minutes,
            max_stale_ms=cfg.max_stale_ms, paper=self.book,
            book_lead_tolerance_ms=cfg.book_lead_tolerance_ms)
        self.last_end: dict[str, int] = {}
        self.last_1h_end: dict[str, int] = {}
        self.last_price: dict[str, float] = {}
        self._poll_ok = True
        self._stance_day = ""
        self._submits: list[int] = []
        self.steps = 0
        self.cycles = 0
        self.feed_failures = 0

    # ------------------------------------------------------------------ the providers' facts

    def _health(self) -> Optional[ExchangeHealth]:
        lat = self.feed.last_latency_ms
        if lat is None:
            return None                                       # nothing observed yet: unknown, fails closed
        return ExchangeHealth(self._poll_ok, f"REST {'ok' if self._poll_ok else 'FAILED'}, {lat:.0f}ms")

    def _kill(self) -> bool:
        return self.ctl.engaged or (self.cfg.out_dir / "V2_KILL").exists()

    def _legacy(self, symbol: str, as_of_ms: int) -> Optional[str]:
        pos = self.book.positions.get(symbol)
        return self.stances.legacy_action(symbol, "none" if pos is None else pos.side.lower(), as_of_ms)

    # ------------------------------------------------------------------ priming

    def prime(self) -> None:
        """History first: the feature engine needs 241 closed 1m bars and 120 1h bars per symbol, and the stance
        source needs ~1000 daily bars. Nothing here triggers a decision."""
        off = self.feed.sync_clock()
        now = self.feed.now_ms()
        log.info("clock offset to the exchange: %+d ms", off)
        for sym in self.cfg.symbols:
            for b in self.feed.history(sym, "1h", N_1H + PRIME_MARGIN, now_ms=now):
                self.pipeline.store.append(b)
                self.last_1h_end[sym] = b.end_ms
            for b in self.feed.history(sym, "1m", N_1M + PRIME_MARGIN, now_ms=now):
                self.pipeline.store.append(b)
                self.last_end[sym] = b.end_ms
                self.book.last[sym] = b.close
                self.last_price[sym] = b.close
        self._refresh_stances(now)
        self._poll_ok = True

    def _refresh_stances(self, now_ms: int) -> None:
        syms = self.cfg.stance_symbols or self.cfg.symbols
        daily = {}
        for s in sorted(set(syms) | {"BTCUSDT"}):
            try:
                daily[s] = self.feed.history(s, "1d", 1000, now_ms=now_ms)
            except FeedError:
                log.warning("daily history for %s unavailable: stances may be UNAVAILABLE", s)
        self.stances.refresh(daily, now_ms)
        self._stance_day = _day(now_ms)

    # ------------------------------------------------------------------ one step

    def step(self) -> list[dict]:
        self.steps += 1
        now = self.feed.now_ms()
        self._poll_ok, recs = True, []
        if _day(now) != self._stance_day:
            self._refresh_stances(now)
        for sym in self.cfg.symbols:
            try:
                # a FRESH "now" per symbol: earlier symbols' Jev calls and fetches take seconds, and a decision must be
                # judged against the moment it is actually made, not the moment the step began
                recs += self._step_symbol(sym, self.feed.now_ms())
            except FeedError as exc:
                self._poll_ok = False
                self.feed_failures += 1
                log.warning("feed failure for %s: %s", sym, type(exc).__name__)
        self.cycles += len(recs)
        self._observe(now)
        self._heartbeat(now)
        return recs

    def _step_symbol(self, sym: str, now: int) -> list[dict]:
        last = self.last_end.get(sym, 0)
        missing = max(2, (now - last) // MIN_MS + 2)
        bars = [b for b in self.feed.klines(sym, "1m", min(1000, missing), now_ms=now) if b.end_ms > last]
        out = []
        for b in sorted(bars, key=lambda x: x.end_ms):
            if b.end_ms % HOUR_MS == 0:                                  # the hourly bar that closes with this minute
                for hb in self.feed.klines(sym, "1h", 3, now_ms=now):
                    if hb.end_ms > self.last_1h_end.get(sym, 0):
                        self.pipeline.on_bar(hb, now)
                        self.last_1h_end[sym] = hb.end_ms
            queued_before = self.book.stats["queued"]
            rec = self.pipeline.on_bar(b, now)
            self._submits += [now] * (self.book.stats["queued"] - queued_before)
            self.last_end[sym] = b.end_ms
            self.last_price[sym] = b.close
            if rec is not None:
                out.append(rec)
        return out

    def _observe(self, now: int) -> None:
        sym0 = self.cfg.symbols[0]
        self._submits = [t for t in self._submits if now - t <= MIN_MS]
        age = max((now - e for e in self.last_end.values()), default=None)
        prev = getattr(self, "_prev_price", None)
        self.ctl.observe(Observation(
            now_ms=now, exchange=self._health(), feed_age_ms=age, orders_last_minute=len(self._submits),
            unexpected_positions=(), recent_slippage_bps=None, price=self.last_price.get(sym0), prev_price=prev,
            daily_pnl=self.book.daily_pnl()))
        self._prev_price = self.last_price.get(sym0)

    def _heartbeat(self, now: int) -> None:
        s = self.pipeline.summary()
        hb = {"now_ms": now, "iso": datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat(),
              "steps": self.steps, "cycles": self.cycles, "feed_failures": self.feed_failures,
              "feed_latency_ms": self.feed.last_latency_ms, "kill_engaged": self._kill(),
              "kill_reasons": list(self.ctl.reasons), "paused": [list(x) for x in self.ctl.paused_scopes],
              "paper_equity": self.book.equity(), "positions": sorted(self.book.positions),
              "jev_calls": self.adapter.stats["calls"], "jev_ok": self.adapter.stats["jev"],
              "jev_fallback": self.adapter.stats["fallback"], "jev_share": s.get("jev_share"),
              "outcomes": {k[8:]: v for k, v in s.items() if k.startswith("outcome:")},
              "models": {k[6:]: v for k, v in self.adapter.stats.items() if k.startswith("model:")}}
        usage = getattr(self.client, "usage", None)
        if usage is not None:
            hb["jev_usage"] = dict(usage)
        (self.cfg.out_dir / "heartbeat.json").write_text(json.dumps(hb, indent=1, sort_keys=True, default=str))

    # ------------------------------------------------------------------ loop

    def stop_requested(self) -> bool:
        return (self.cfg.out_dir / "STOP").exists()

    def run(self, max_steps: Optional[int] = None, stop_at_ms: Optional[int] = None) -> None:
        self.prime()
        while not self.stop_requested():
            if max_steps is not None and self.steps >= max_steps:
                break
            now = self.feed.now_ms()
            if stop_at_ms is not None and now >= stop_at_ms:
                break
            wait = (MIN_MS - now % MIN_MS) / 1000.0 + self.cfg.grace_s     # just after the next minute closes
            self._sleep(wait)
            recs = self.step()
            for r in recs:
                log.info("cycle %s %s outcome=%s", r["symbol"], r["ts_iso"], r["outcome"])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="V2 paper runner (public data, simulated book; no real orders)")
    ap.add_argument("--demo", action="store_true", help="required: use the UNVALIDATED demo limits")
    ap.add_argument("--symbols", default="BTCUSDT")
    ap.add_argument("--cadence", type=int, default=15, help="minutes between decisions")
    ap.add_argument("--out", default="data/local/v2")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes")
    ap.add_argument("--workflow", default="crypto_trading_v2.json")
    a = ap.parse_args(argv[1:])
    if not a.demo:
        print("refusing to run without --demo: the risk, kill and exit limits are the owner's decisions (E7, E10);\n"
              "--demo uses clearly UNVALIDATED placeholder values for a paper-only run.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = demo_config(tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip()), a.cadence, Path(a.out))
    wf = load_workflow(WORKFLOW_DIR / a.workflow)
    wf.require_wire_ready()
    runner = Runner(cfg, BinancePublic(), wf)
    stop_at = int(time.time() * 1000 + a.minutes * MIN_MS) if a.minutes else None
    log.warning("PAPER run with UNVALIDATED DEMO limits; symbols=%s cadence=%dmin; Jev key present: %s",
                cfg.symbols, cfg.cadence_minutes, bool(__import__("os").environ.get("TYPESAFE_API_KEY", "").strip()))
    try:
        runner.run(max_steps=a.max_steps, stop_at_ms=stop_at)
    except KeyboardInterrupt:
        log.warning("interrupted")
    print(json.dumps(json.loads((cfg.out_dir / "heartbeat.json").read_text()), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
