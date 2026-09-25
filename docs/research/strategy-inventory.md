# Strategy inventory: what already exists in this repo

Task T1.4 · 2026-09-26 · Read from module docstrings, code and `config/crontab.*`. Nothing was executed.
"Scheduled" means the line exists in a crontab file in git. **Which crontab is live on the VPS is
unknown** (`crontab.trade` and `crontab.backup` differ; E1 asks for `crontab -l`).

## Real-money books (order code; the loop never runs or edits these)

| Script | Market | Signal | Scheduled in | Gate | Claimed evidence |
|---|---|---|---|---|---|
| `scripts/trend_follower.py` | Binance spot, `TREND_REAL=1` | coin > SMA50 **and** BTC regime; exit on trend break or −15% hard stop; 3 positions, $5.5 each | `crontab.backup:41` (every 4h) | env var only | `SMA_N` comment: SMA50 "beat 100/150/200 OOS"; the owner confirms 3 open positions (2026-09-25). Defects F1–F3 (designs T0.3–T0.5) |
| `scripts/futures_executor.py` | Binance USDT-M futures, **3x** | "the SAME elite signals as spot (15m SHARP on SOL/XRP)" | `crontab.backup:31` (every 10 min) | **none**: runs if `config/live.env` exists | docstring: account funded with about $6 |
| `scripts/lev_satellite_manage.py` | Binance futures, 3x SOL | exit on SOL < daily SMA50, +25% TP on margin, or liquidation | `crontab.backup:43` (every 4h) | **none** | SYSTEM_MAP: "3x SOL satellite, $3 cap, −$0.10" |
| `scripts/live_executor.py` | Binance spot | called by `unified_trader` | `crontab.trade:14` (manage, every 10 min) | `config/LIVE_ENABLED` (git holds only a `.retired` copy) | defect F4 (T0.6) |

`futures_executor.py` and `lev_satellite_manage.py` were **not on the never-run list** the owner gave. They
are added to the loop's boundary in a separate PR (T1.4b).

## Paper / demo books

| Script | Venue | Signal | Scheduled | Claimed evidence | Where the evidence lives |
|---|---|---|---|---|---|
| `scripts/unified_trader.py` | paper, plus real via `live_executor` when allowed | capitulation dip (deep dip + low RSI + red bars), multi-timeframe; "ELITE" 15m SHARP on ETH/SOL/XRP | both crontabs, :16/:46 | "152-trade paper ledger: 5m scalps 69% WR +$0.37; 15m 60% WR +$0.14; 30m coin-flip" (`:68`); "15m SHARP ETH 87-88%, SOL 80-90%, XRP 68-93%" (`:62`) | `data/unified-paper.json` (prod); the backtest isn't in git |
| `scripts/adaptive_engine.py` | paper ($500 budget) | regime detector → capitulation catch (BTC 30m: dip −2%, high vol, 3 red, RSI ≤ 30, hold 6h); Kelly-capped sizing | both crontabs, hourly | "70.2% WR, +0.44% EV, PF 2.92, walk-forward stable" (`docs/adaptive-framework.md:103`) | `data/adaptive-state.json`, `data/regime-edges.json` |
| `scripts/regime_trader.py` | Bybit demo | BTC > SMA200: long strongest N coins above SMA50; bear: short weakest | `crontab.backup:52` (every 4h) | "24 coins, 1000 days, fees 0.144%, IS/OOS: full +29.7%, OOS +40.8%, maxDD −21.9%; BTC −35.2% same OOS" (docstring) | backtest code: `research/03-synthesis.py`? (to confirm) |
| `src/trading/allocation/testnet_executor.py` | Binance testnet | P-A accumulation: monthly buy, 1.5x on −15% drawdown | `ecosystem.config.js` (hourly) | — | `data/pa-paper-state.json` |
| `src/trading/allocation/dual_executor.py` | paper + tiny live | confidence-gated P-A entries | not found in crontabs | — | — |
| `scripts/signal_watch.py`, `scripts/watchdog.py`, `src/trading/signals/auto_trader.py` | testnet | 7 detectors (dip zone, deep dip, regime shift, momentum break...); auto-trade on BIG signals | `ecosystem.config.js` (4h), pm2 daemon (30s) | — | — |
| `scripts/gold_scanner.py` | signals only (Telegram) | XAU buy-dip, SL at the dip low, TP 2R; records WIN/LOSS/TIMEOUT | `crontab.backup:33` (every 15 min) | self-tracking record | its own state file |
| `scripts/scalping_sweep.py` | research | BTC 15m/30m capitulation variants, 80/20 split | not scheduled | reads `/tmp/live_fine.json` (not in git) | — |
| `scripts/bybit_demo_trader.py` | Bybit demo | long-only trend | **retired 2026-09-13** (`crontab.backup:45`) | "no proven edge" | — |

## Contradictions that must be settled before any of these becomes a baseline

1. **Dip-buying / capitulation.** `docs/adaptive-framework.md:103` says 70.2% WR, +0.44% EV, PF 2.92,
   walk-forward stable (BTC 30m, 6h hold). `SYSTEM_MAP.md:172` says "Dip-buying / accuracy: 66% win rate still LOSES".
   These can both be true: the framework doc itself records an "84% → 66% collapse" from a single split
   (`adaptive-framework.md:95`), and the 70.2% figure is the later walk-forward result for **BTC only** ("alts are negative
   expectancy"). Neither states the fee assumption next to the number. **Resolution:** re-run the capitulation rule under
   the T3.3 gate (shared account, fees from T1.1, BTC benchmark, regime stated) before treating it as a baseline.
2. **`unified_trader` win rates vs fees.** 69% WR on 5m scalps with +$0.37 total over its share of 152 trades is not
   evidence of edge until checked against its TP/SL and costs. For 0.5/0.5% exits at VIP 0 with 5bp slip,
   break-even is 80% (T1.1). T1.5 plans that check.
3. **"ELITE 15m SHARP 87–93%"** comes from a backtest over 600–800 candles per symbol (`unified_trader.py:61-62`), a small
   sample with no stated OOS split. The repo's own longer study contradicts it: `futures_executor.py:182-184` records a
   "3.6y / 15m OOS study (2026-09-13)" in which **unfiltered SHARP dips turn negative out of sample (BTC −5.4%)**, and only
   an added 15m EMA200 filter makes it positive (+13.5%), lifting **SOL WR from 51% to 61%**, not 80–90%. So the
   headline figure is an in-sample number, and the evidenced version is "about 61% WR with the EMA200 filter, before a fee
   check". `futures_executor` trades these signals **at 3x with real money**, with the filter enforced in
   `futures_open()` since a bypass bug was fixed on 2026-09-13 (`:179-182`). This is the highest-risk claim in the repo;
   T1.5 re-checks it against fees (61% vs the break-even p* for its exits).
4. **Is `regime_trader` the "not yet deployed" edge?** SYSTEM_MAP §8 says the regime-adaptive edge is "NOT YET DEPLOYED
   ANYWHERE", yet `crontab.backup:52` schedules `regime_trader.py` on the Bybit demo. Either SYSTEM_MAP is stale or
   that crontab isn't live. E1's `crontab -l` settles it.

## What this means for Phase 3

- Baselines to port as read-only plugins (T3.4): trend_follower's signal, regime_trader's signal, the capitulation catch.
  Each is re-scored under the T3.3 gate. No inherited win rate is carried over.
- `unified_trader`'s ELITE claim goes to the top of the verification queue (T1.5), because real 3x futures money depends on it.
