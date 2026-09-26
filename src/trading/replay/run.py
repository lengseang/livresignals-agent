"""Run a REPLAY of the trend baseline over history on disk (research only, not evidence of edge).

    python -m trading.replay.run --history-root data/local/history/monthly --symbols BTCUSDT,ETHUSDT

Defaults are the pre-registered baseline run in docs/loop/designs/T3.0-T3.1.md. The holdout (last 6
complete months on disk) is refused unless --open-holdout is passed. Every run is appended to
<out>/trials.jsonl so the Deflated Sharpe gate (T3.3) can count it as a trial.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from trading.paper.broker import KlineBroker
from trading.paper.fills import FillConfig
from trading.replay import loader, metrics, windows
from trading.replay.broker import CostModel, PaperAccount, iso
from trading.replay.engine import replay
from trading.replay.plugins.trend_sma import TrendSMA

LABEL = "REPLAY (simulated; research, not evidence of edge)"


def _pct(x) -> str:
    return "UNKNOWN" if x is None else f"{x * 100:+.2f}%"


def _dd(x) -> str:
    return "UNKNOWN" if x is None else f"-{x * 100:.2f}%"


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history-root", default="data/local/history/monthly")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--benchmark", default="BTCUSDT")
    ap.add_argument("--warmup-start", default="2017-08")
    ap.add_argument("--trade-start", default="2017-11")
    ap.add_argument("--end", default=None, help="exclusive end month; default = holdout start")
    ap.add_argument("--is-oos-split", default="2022-01")
    ap.add_argument("--wf-fit", type=int, default=24)
    ap.add_argument("--wf-trade", type=int, default=12)
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--slots", type=int, default=None, help="default = number of symbols")
    ap.add_argument("--sma", type=int, default=50)
    ap.add_argument("--btc-sma", type=int, default=50)
    ap.add_argument("--fee", type=float, default=0.001)
    ap.add_argument("--spread-bps", type=float, default=2.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--stress-slippage-bps", type=float, default=5.0)
    ap.add_argument("--broker", choices=("cost", "paper"), default="cost",
                    help="cost = legacy flat spread/slippage assumptions (the pre-registered T3.1 run); "
                         "paper = the measured fill model (T4.2): per-symbol median spread, p90 as the stress run")
    ap.add_argument("--fill-cap-usd", default="5",
                    help="paper broker: per-fill notional cap ('none' = uncapped, labelled depth_unmodelled)")
    ap.add_argument("--over-cap", choices=("clip", "label"), default="clip",
                    help="paper broker: clip entries to the cap, or fill them and flag depth_unmodelled")
    ap.add_argument("--out", default="data/local/replay")
    a = ap.parse_args(argv)

    root, syms = Path(a.history_root), a.symbols.split(",")
    last_complete = min(loader.month_files(root, s)[-1][0] for s in syms)
    hold = windows.holdout_start(last_complete)
    end_ym = a.end or hold
    t_warm, t_trade, t_end = windows.ym_ms(a.warmup_start), windows.ym_ms(a.trade_start), windows.ym_ms(end_ym)
    windows.check_window(t_trade, t_end, hold, a.open_holdout)

    slots = a.slots or len(syms)
    if a.broker == "paper":
        cap = None if a.fill_cap_usd.lower() == "none" else float(a.fill_cap_usd)
        base = FillConfig(fee_taker=a.fee, fee_maker=a.fee, fill_cap_usd=cap, over_cap=a.over_cap)
        cfgs = {"gross": replace(base, fee_taker=0.0, fee_maker=0.0, spread_stat="none"),
                "net": base, "stress": replace(base, spread_stat="p90")}
        costs = {k: c.header() for k, c in cfgs.items()}

        def make(k, n, name):
            return KlineBroker(a.equity, n, cfgs[k], name=name, symbols=syms + [a.benchmark])
    else:
        cms = {"gross": CostModel(0, 0, 0),
               "net": CostModel(a.fee, a.spread_bps, a.slippage_bps),
               "stress": CostModel(a.fee, a.spread_bps, a.stress_slippage_bps)}
        costs = {k: vars(c) for k, c in cms.items()}

        def make(k, n, name):
            return PaperAccount(a.equity, n, cms[k], name=name)
    accts = {k: make(k, slots, k) for k in ("net", "gross", "stress")}
    bench = {k: (a.benchmark, make(k, 1, f"bh_{k}")) for k in ("gross", "net", "stress")}
    strat = TrendSMA(a.sma, a.btc_sma, btc=a.benchmark)
    run_id = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{strat.name}"
              + ("-paper" if a.broker == "paper" else ""))

    data_stats: dict = {}
    streams = {s: loader.iter_symbol(root, s, start_ms=t_warm, end_ms=t_end, stats=data_stats) for s in syms}
    t = time.time()
    res = replay(streams, [strat], accts, trade_start_ms=t_trade, end_ms=t_end, run_id=run_id, benchmarks=bench)
    secs = time.time() - t

    btc_daily = res.store.all(a.benchmark, "1d")
    t_last = res.ts[-1] if len(res.ts) else t_trade
    periods = {"full": (t_trade, t_last), "IS": (t_trade, windows.ym_ms(a.is_oos_split)),
               "OOS": (windows.ym_ms(a.is_oos_split), t_last)}
    for w in windows.walk_forward(a.warmup_start, end_ym, a.wf_fit, a.wf_trade):
        if windows.ym_ms(w.trade_start) >= t_trade:
            periods[f"WF {w.trade_start}..{w.trade_end}"] = (windows.ym_ms(w.trade_start), windows.ym_ms(w.trade_end))

    def acct_block(acc: PaperAccount) -> dict:
        per = {}
        for name, (p0, p1) in periods.items():
            per[name] = metrics.window_stats(res.ts, acc.curve, acc.start_equity, p0, p1)
        return {"final_equity_mid": acc.equity(), "final_equity_liquidated": acc.liquidation_value(),
                "return_liquidated": acc.liquidation_value() / acc.start_equity - 1,
                "max_dd_1m": acc.max_drawdown(), "sharpe_daily_ann": metrics.daily_sharpe(res.ts, acc.curve),
                "fills": acc.fills, "open_positions": sorted(acc.positions), "periods": per,
                **metrics.trade_stats(acc.trades)}

    fills = {k: v for k, v in res.fills.items()}
    summary = {
        "mode": "REPLAY", "label": LABEL, "run_id": run_id, "strategy": strat.name, "params": strat.params,
        "symbols": syms, "slots": slots, "start_equity": a.equity,
        "broker": a.broker, "costs": costs,
        "fill_model_labels": ({k: v.labels() for k, v in accts.items()} if a.broker == "paper" else None),
        "window": {"warmup_from": iso(t_warm), "trade_from": iso(t_trade), "to": iso(t_last),
                   "holdout_from": hold, "holdout_opened": a.open_holdout, "last_complete_month": last_complete},
        "data_first_bar": {s: iso(b.open_ms) for s, b in res.first_open.items()},
        "data_anomalies": data_stats,
        "signals": res.signals, "warmup_signals": res.warmup_signals, "fills": fills,
        "scenarios_consistent": len(set(fills.values())) == 1,
        "accounts": {k: acct_block(v) for k, v in accts.items()},
        "buy_and_hold": {k: acct_block(v) for k, (_, v) in bench.items()},
        "regime": {name: metrics.regime(btc_daily, p0, p1) for name, (p0, p1) in periods.items()},
        "replay_seconds": round(secs, 1), "steps": res.steps,
        "caveats": [
            "REPLAY results are research, not evidence of edge.",
            ("Spread = measured per-symbol median (net) / p90 (stress) from a 7.56 h sample (spreads_v1.json); "
             "no slippage beyond L1 is modelled (bars have no depth); the real fee rate is UNKNOWN."
             if a.broker == "paper" else
             "Spread and slippage are assumptions (UNKNOWN until T2.3 measures them); the real fee rate is UNKNOWN."),
            "SMA50 was chosen by the live script's author over 100/150/200 on this same history: not clean OOS.",
            "Universe is today's survivors (survivorship bias).",
            "Per-period numbers are slices of one continuous run, marked to mid at 1m closes.",
            "Port differs from scripts/trend_follower.py: no -15% hard stop, daily not 4-hourly checks.",
        ],
    }

    out = Path(a.out) / run_id
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for e in res.signal_events:
            f.write(json.dumps(e) + "\n")
        for acc in list(accts.values()) + [v for _, v in bench.values()]:
            for e in acc.events:
                f.write(json.dumps(e) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with open(Path(a.out) / "trials.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"mode": "REPLAY", "run_id": run_id, "strategy": strat.name, "params": strat.params,
                            "broker": a.broker,
                            "symbols": syms, "trade_from": iso(t_trade), "to": iso(t_last),
                            "holdout_opened": a.open_holdout}) + "\n")

    if a.broker == "paper":
        for lab in accts["net"].labels():
            print(f"[fill model] {lab}")
    print(f"{LABEL}\nrun {run_id}: {res.steps} steps in {secs:.0f}s; window {iso(t_trade)} -> {iso(t_last)}; "
          f"holdout from {hold} {'OPENED' if a.open_holdout else 'untouched'}")
    print("| period | strategy gross | strategy net | net stress | net max DD | BTC B&H gross | BTC B&H net | "
          "B&H max DD | BTC regime |")
    print("|---|---|---|---|---|---|---|---|---|")
    A, H = summary["accounts"], summary["buy_and_hold"]
    for name in periods:
        rg = summary["regime"][name]
        reg = rg["label"] + (f" (BTC {_pct(rg['btc_return'])}, vol {rg['btc_ann_vol'] * 100:.0f}%, "
                             + ("above SMA200 on UNKNOWN share of days" if rg["share_days_above_sma200"] is None else
                                f"above SMA200 on {rg['share_days_above_sma200'] * 100:.0f}% of "
                                f"{rg['days_with_sma200']} days") + ")" if "btc_return" in rg else "")
        print(f"| {name} | {_pct(A['gross']['periods'][name]['ret'])} | {_pct(A['net']['periods'][name]['ret'])} | "
              f"{_pct(A['stress']['periods'][name]['ret'])} | {_dd(A['net']['periods'][name]['max_dd'])} | "
              f"{_pct(H['gross']['periods'][name]['ret'])} | {_pct(H['net']['periods'][name]['ret'])} | "
              f"{_dd(H['net']['periods'][name]['max_dd'])} | {reg} |")
    n, h = A["net"], H["net"]

    def _r(x):
        return "UNKNOWN" if x is None else round(x, 2)
    print(f"net: closed trades {n['closed']}, fills {n['fills']} (per scenario {fills}), open at end "
          f"{n['open_positions']}, win rate {_pct(n['win_rate_net'])}, avg win {_pct(n['avg_win_net'])}, "
          f"avg loss {_pct(n['avg_loss_net'])}, fees paid {n.get('fees_paid', 0):.2f} on {a.equity:.0f} start, "
          f"return after liquidation {_pct(n['return_liquidated'])} (gross {_pct(A['gross']['return_liquidated'])}), "
          f"max DD {_dd(n['max_dd_1m'])}, sharpe(daily, ann.) {_r(n['sharpe_daily_ann'])}")
    print(f"BTC buy&hold net: return after liquidation {_pct(h['return_liquidated'])} (gross "
          f"{_pct(H['gross']['return_liquidated'])}), max DD {_dd(h['max_dd_1m'])}, sharpe {_r(h['sharpe_daily_ann'])}")
    print(f"data anomalies (dropped rows): {data_stats or 'none'}")
    print(f"wrote {out}")
    return summary


if __name__ == "__main__":
    main()
