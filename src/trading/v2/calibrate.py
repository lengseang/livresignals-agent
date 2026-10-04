"""Calibrate the owner's open numbers (E7, E10) FROM MEASURED MARKET DATA, reproducibly. Read-only, no orders.

These are PROPOSALS, not approvals: every value is derived by a stated rule from public data, so the owner can accept it, change
the rule, or override the number. Rules (each is a judgement; the evidence is returned beside the value):
  kill.max_price_move_frac   3 x the largest 1-minute close-to-close move seen in the sample (all symbols), clamped to [2%, 15%],
                             rounded UP to 0.5%. A one-minute move beyond that is treated as a corrupt feed.
  paper spread / slippage    spread = the worst per-symbol MEDIAN bookTicker spread sampled; slippage = 1 bp (an assumption).
  risk.max_spread_bps        max(5, 4 x the worst median spread), rounded up (the spec's example cap is 5 bps).
  stop_pct / target_pct      stop = 3 x the 90th-percentile hourly ATR(14)/close of the widest symbol, clamped to [1%, 5%] (5% is the
                             legacy max stop), rounded up to 0.5%; target = 2 x stop.
  daily loss cap             2% of paper equity for the risk engine (tighter than the legacy 5% halt: one 2% stop-out at 100%
                             notional uses it up); the kill switch latches at twice that.
  daily_loss_budget_usd      equity x the legacy 2% risk-per-trade = one full stop-out, so the legacy "2x budget used" breaker
                             means two full stop-outs in a day (the legacy definition is not written down: E8).
Fees stay at the repo's 0.10% (the account's real rate is UNKNOWN). Nothing here is validated against outcomes: shadow/paper results are.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from trading.replay.broker import CostModel
from trading.signals.indicators import atr
from trading.v2.binance_public import MAX_LIMIT, BinancePublic
from trading.v2.killswitch import KillLimits
from trading.v2.policy import ExitParams
from trading.v2.risk import RiskLimits
from trading.v2.runner import RunnerConfig


def _up(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-9) * step, 10)


@dataclass(frozen=True)
class Calibration:
    symbols: tuple[str, ...]
    equity: float
    max_1m_move: float
    p999_1m_move: float
    median_spread_bps: dict
    atr1h_pct_p90: dict
    one_min_bars: int
    # the proposed values
    kill_max_price_move_frac: float
    paper_spread_bps: float
    paper_slippage_bps: float
    risk_max_spread_bps: float
    stop_pct: float
    target_pct: float
    daily_loss_cap_usd: float
    kill_daily_loss_cap_usd: float
    daily_loss_budget_usd: float


def calibrate(feed: BinancePublic, symbols: tuple[str, ...], *, equity: float = 1_000.0, one_min_pages: int = 6,
              spread_samples: int = 15, sleep: Callable[[float], None] = time.sleep) -> Calibration:
    now = feed.now_ms()
    moves: list[float] = []
    atr_p90: dict[str, float] = {}
    for sym in symbols:
        end = None
        for _ in range(one_min_pages):                                   # ~16.7 h per page of 1000 one-minute bars
            chunk = feed.klines(sym, "1m", MAX_LIMIT, end_time_ms=end, now_ms=now)
            if len(chunk) < 2:
                break
            moves += [abs(b.close / a.close - 1.0) for a, b in zip(chunk, chunk[1:])]
            end = chunk[0].open_ms - 1
        h = feed.history(sym, "1h", 500, now_ms=now)
        rows = [{"open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in h]
        a = atr(rows, 14)
        pct = sorted(x / r["close"] for x, r in zip(a, rows[13:]))
        atr_p90[sym] = pct[int(0.9 * (len(pct) - 1))] if pct else float("nan")
    spreads: dict[str, list[float]] = {s: [] for s in symbols}
    for i in range(spread_samples):
        for sym in symbols:
            b = feed.book(sym)
            spreads[sym].append((b.ask - b.bid) / ((b.ask + b.bid) / 2) * 1e4)
        if i + 1 < spread_samples:
            sleep(1.0)
    med = {s: statistics.median(v) for s, v in spreads.items()}
    moves.sort()
    max_move = moves[-1] if moves else float("nan")
    p999 = moves[int(0.999 * (len(moves) - 1))] if moves else float("nan")
    worst_spread = max(med.values())
    widest_atr = max(atr_p90.values())
    stop = min(0.05, max(0.01, _up(3 * widest_atr, 0.005)))
    return Calibration(
        symbols=symbols, equity=equity, max_1m_move=max_move, p999_1m_move=p999, median_spread_bps=med,
        atr1h_pct_p90=atr_p90, one_min_bars=len(moves) + len(symbols),
        kill_max_price_move_frac=min(0.15, max(0.02, _up(3 * max_move, 0.005))),
        paper_spread_bps=round(worst_spread, 4), paper_slippage_bps=1.0,
        risk_max_spread_bps=max(5.0, _up(4 * worst_spread, 1.0)),
        stop_pct=stop, target_pct=round(2 * stop, 4),
        daily_loss_cap_usd=round(0.02 * equity, 2), kill_daily_loss_cap_usd=round(0.04 * equity, 2),
        daily_loss_budget_usd=round(0.02 * equity, 2))


def to_runner_config(c: Calibration, cadence_minutes: int, out_dir: Path) -> RunnerConfig:
    """A RunnerConfig from the PROPOSED numbers. Still paper-only and still unapproved."""
    return RunnerConfig(
        symbols=c.symbols, cadence_minutes=cadence_minutes, paper_equity=c.equity, out_dir=out_dir,
        risk_limits=RiskLimits(daily_loss_cap_usd=c.daily_loss_cap_usd, max_spread_bps=c.risk_max_spread_bps),
        kill_limits=KillLimits(exchange_failures_to_engage=3, max_feed_age_ms=180_000,
                               max_orders_per_minute=max(5, 2 * len(c.symbols)), max_abs_slippage_bps=50.0,
                               max_price_move_frac=c.kill_max_price_move_frac,
                               daily_loss_cap_usd=c.kill_daily_loss_cap_usd, min_request_confidence=0.8),
        exit_params=ExitParams(stop_pct=c.stop_pct, target_pct=c.target_pct),
        daily_loss_budget_usd=c.daily_loss_budget_usd,
        costs=CostModel(fee_rate=0.001, spread_bps=c.paper_spread_bps, slippage_bps=c.paper_slippage_bps))


def main(argv: list[str]) -> int:
    """Print the proposal; with --run also start a PAPER session with it (live public data, real Jev if a key is set)."""
    import argparse
    import dataclasses
    import json
    import logging
    import sys
    from trading.v2.runner import Runner
    from trading.v2.workflow import WORKFLOW_DIR, load_workflow
    ap = argparse.ArgumentParser(description="Calibrate V2's open numbers from live public data (proposals, not approvals)")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--run", action="store_true", help="then run a PAPER session with the proposed numbers")
    ap.add_argument("--cadence", type=int, default=15)
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--out", default="data/local/v2")
    a = ap.parse_args(argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    feed = BinancePublic()
    feed.sync_clock()
    syms = tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip())
    cal = calibrate(feed, syms, equity=a.equity)
    print(json.dumps(dataclasses.asdict(cal), indent=1, default=str))
    if not a.run:
        return 0
    wf = load_workflow(WORKFLOW_DIR / "crypto_trading_v2.json")
    runner = Runner(to_runner_config(cal, a.cadence, Path(a.out)), feed, wf)
    stop_at = int(time.time() * 1000 + a.minutes * 60_000) if a.minutes else None
    try:
        runner.run(stop_at_ms=stop_at)
    except KeyboardInterrupt:
        pass
    print(json.dumps(json.loads((Path(a.out) / "heartbeat.json").read_text()), indent=1))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
