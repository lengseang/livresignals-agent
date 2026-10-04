# V2 Service Contracts

> Status: **DESIGN** — specifies the V2 architecture. Nothing here is implemented yet,
> and nothing here may be implemented on the live path without explicit owner approval.
>
> Source: the V2 architecture note (Notion, "HER"), rewritten as implementable contracts.
> Every contract below is grounded in what is *actually running today* on
> `154.219.105.99`, not in an idealised rebuild. Where a V2 component replaces an
> existing one, the existing one is named.
>
> Core rule, restated because everything else depends on it:
> **Hermes controls the intelligence and the workflow. It never controls money.**

---

## 0. Why the current shape is being replaced

Today the loop is:

```
cron → Hermes → DeepSeek → write function → execute
```

That is slow, expensive, and — critically — **an LLM is in the hot path of every
decision**. When the provider is unreachable (this has happened: MOYU token revoked,
OpenCode free tier refusing external calls), *there is no decision at all*. Trading
stops silently.

V2 keeps the LLM where it adds value (research, strategy development, diagnosis) and
puts a **fast, typed decision primitive** plus a **deterministic risk engine** in the
hot path.

```
Hermes (System 2)   research / strategy / code / diagnosis     — not in hot path
Jev (System 1)      fast typed decisions on every evaluation   — hot path, fallback-guarded
Risk Engine         deterministic veto                          — hot path, no LLM
Execution           orders                                      — no LLM
```

**Neither Jev nor Hermes gets exchange credentials.**

---

## Contract A — Market Event

Produced by the market data layer. Consumed by the feature engine.

Every downstream number is derived from this, so it carries its own provenance.

```json
{
  "contract": "market_event",
  "version": "1.0",
  "event_id": "mkt_01J8Z...",
  "symbol": "BTCUSDT",
  "venue": "binance",
  "event_time_utc": "2026-10-04T11:15:00.000Z",
  "received_time_utc": "2026-10-04T11:15:00.120Z",
  "source": "ws|rest|ingest_all.py",
  "kind": "trade|book|kline_closed",
  "price": 108420.5,
  "size": 0.014,
  "bid": 108420.0,
  "ask": 108420.8,
  "spread_bps": 1.2
}
```

**Rules**

- `kline_closed` events fire **only** on bar close. Never mid-bar — a mid-bar signal is
  a signal on an unfinished number.
- `received_time_utc` is mandatory. The gap to `event_time_utc` is feed latency and is
  monitored; a strategy that only wins at zero latency is not a strategy.
- `event_id` is stable and unique, so the event bus can de-duplicate on replay.

**Replaces.** Today `ingest_all.py` writes candles straight into SQLite
(`data/trading.db`, 9.5M rows) and readers query it. V2 adds the event stream in front;
the SQLite archive remains the historical store.

---

## Contract B — Feature Snapshot

Produced by the feature engine from a `market_event`. Consumed by the state builder.

Features are **computed, not fetched**. Anything fetched belongs in Contract A.

```json
{
  "contract": "feature_snapshot",
  "version": "1.0",
  "snapshot_id": "feat_01J8Z...",
  "symbol": "BTCUSDT",
  "as_of_utc": "2026-10-04T11:15:00.000Z",
  "returns": { "1m": 0.0012, "5m": 0.0041, "15m": 0.0078, "1h": 0.012, "4h": 0.027 },
  "technical": { "rsi": 67.8, "atr": 0.014, "ema_fast_slope": 0.003,
                 "ema_slow_slope": 0.002, "distance_vwap": 0.008 },
  "volume": { "volume_1m": 1823.2, "volume_zscore": 2.71 },
  "orderbook": { "imbalance": 0.31, "bid_depth": 12.8, "ask_depth": 9.7 },
  "regime": { "trend": "bullish", "volatility": "high", "liquidity": "normal" },
  "data_quality": { "complete": true, "missing_fields": [], "stale_ms": 120 }
}
```

**Rules**

- `data_quality.complete` must be checked before the snapshot is used. A snapshot with
  missing fields is **not** a weak signal — it is an invalid one. Reject it.
- `stale_ms` above a configured ceiling rejects the snapshot. Stale features are how a
  system makes a confident decision on yesterday's market.
- Feature definitions are versioned with the snapshot. A changed RSI definition silently
  invalidates every backtest that used the old one.

**Exists today, partially.** `unified_trader.py` and the scanners compute features
inline. V2 makes them a named contract so the regime classifier and the strategies
consume the *same* numbers.

---

## Contract C — Decision State

The complete input to a decision. Assembled by **our software**, never by Hermes and
never by Jev.

This is the most important contract: if the state is wrong, every decision above it is
wrong, and no amount of model quality recovers it.

```json
{
  "contract": "decision_state",
  "version": "1.0",
  "state_id": "st_01J8Z...",
  "timestamp": 1791083882,
  "symbol": "BTCUSDT",
  "market": { "price": 108420.5, "spread_bps": 1.2 },
  "features": { "...": "Contract B, by reference" },
  "regime": { "trend": "bullish", "volatility": "high", "liquidity": "normal" },
  "position": { "side": "none", "size": 0, "entry_price": null, "unrealized_pnl": 0 },
  "risk": { "equity": 10000, "daily_pnl": -72,
            "daily_loss_limit_remaining": 428, "current_exposure": 0.0 },
  "strategy": { "name": "btc_breakout_v3", "version": "3.2.1" }
}
```

**Rules**

- **Contains no credentials, no account identifiers, no API endpoints.** Jev receives
  state and answers questions. It never holds the ability to act.
- `risk` is the live risk state, not a copy. If the risk engine has already vetoed twice
  today, that is in here.
- The state is **serialisable and loggable in full**. Every decision must be reconstructable
  after the fact from the state alone. A decision that cannot be replayed cannot be audited.

---

## Contract D — Jev Adapter  ⟵ **the fallback lives here**

The boundary between "a decision engine answered" and "we decide ourselves".

```
        decision_state
              │
              ▼
      ┌───────────────┐
      │ JEV ADAPTER   │
      └───────┬───────┘
              │
      ┌───────┴────────┐
      │                │
  Jev answers      Jev unavailable
      │                │
      ▼                ▼
  jev decision    deterministic fallback
  (typed)         (existing validated strategies)
      │                │
      └───────┬────────┘
              ▼
        decision (Contract C output)
```

```json
{
  "contract": "decision",
  "version": "1.0",
  "decision_id": "dec_01J8Z...",
  "state_id": "st_01J8Z...",
  "source": "jev|fallback",
  "fallback_reason": null,
  "action": "BUY|SELL|HOLD|FLAT",
  "confidence": 0.82,
  "questions": {
    "market_regime":  { "answer": "high_volatility_uptrend", "probability": 0.79 },
    "trend_quality":  { "answer": "strong", "probability": 0.84 },
    "entry_quality":  { "answer": "good", "probability": 0.77 },
    "breakout_probability": { "answer": "likely", "probability": 0.68 },
    "reversal_probability": { "answer": "unlikely", "probability": 0.19 },
    "risk_level":     { "answer": "moderate", "probability": 0.61 }
  },
  "latency_ms": 240
}
```

**Rules — the fallback is not optional**

1. **`source` must always be populated.** If `source == "fallback"`, `fallback_reason`
   is mandatory and the decision is **capped at reduced confidence** so it cannot
   trigger the highest-autonomy action path alone.
2. **Fallback triggers on any of:** timeout, HTTP error, malformed JSON, schema
   validation failure, unknown enum value, or Jev balance exhausted.
3. **The fallback is the existing validated strategies** — `trend_follower`,
   `donchian_ensemble`, `tsmom`, `ctrend` — which are already OOS-validated and already
   in the repo. **No new logic is invented under pressure.** A fallback that has never
   been backtested is worse than no fallback, because it looks like one.
4. **The malformed-JSON case is the dangerous one.** A decision engine that returns
   plausible-but-invalid output (`"confidence": "high"`) must be *rejected*, not coerced.
   Coercion is how a string becomes `0.0` becomes a veto becomes a missed trade.
5. `latency_ms` is recorded. If Jev is slow, that is a signal about the design, not just
   about the request.

**Why this contract exists at all.** Every outage on this box has been an external
dependency failing: MOYU revoked, OpenCode refusing, the gateway losing its provider.
Putting Jev unguarded in the hot path would make the *same* failure mode load-bearing.
$5 of Jev is a lot of decisions — but a balance can empty, and a network can drop.

---

## Contract E — Policy Decision

Turns a raw decision into a permitted action, using **thresholds that are configured,
not inferred**.

```json
{
  "contract": "policy_decision",
  "version": "1.0",
  "decision_id": "dec_01J8Z...",
  "policy_version": "1.4",
  "tier": "IGNORE|WATCH|STRATEGY_CHECK|EXECUTE",
  "thresholds_applied": { "ignore_below": 0.55, "watch_below": 0.75,
                          "strategy_check_below": 0.90, "execute_at_or_above": 0.90 },
  "adjusted_confidence": 0.82,
  "escalate_to": null,
  "action": "STRATEGY_CHECK"
}
```

**Tier semantics**

| Confidence | Tier | What happens |
|---|---|---|
| `< 0.55` | `IGNORE` | logged, nothing else |
| `0.55 – 0.75` | `WATCH` | added to the watch set, no position |
| `0.75 – 0.90` | `STRATEGY_CHECK` | escalated to Hermes/DeepSeek for a deeper look |
| `> 0.90` | `EXECUTE` | goes to the risk engine |

**Rules**

- **A `fallback` decision can never reach `EXECUTE`.** It is capped at `STRATEGY_CHECK`.
  This is the mechanism that stops the fallback from becoming a second, weaker hot path.
- Thresholds are **adaptive later**, and when they become adaptive the change goes
  through the same gate as any strategy change (branch → tests → backtest → paper).
  Thresholds tuned on the fly, in production, are not thresholds.
- `policy_version` is recorded on every decision so a historical result can be attributed
  to the rule set that produced it.

---

## Contract F — Trade Proposal

Still no position size.

```json
{
  "contract": "trade_proposal",
  "version": "1.0",
  "proposal_id": "tp_01J8Z...",
  "symbol": "BTCUSDT",
  "side": "LONG|SHORT",
  "strategy": { "name": "btc_breakout_v3", "version": "3.2.1" },
  "intent": "entry|exit|reduce|flatten",
  "reference_price": 108420.5,
  "stop_pct": 0.02,
  "target_pct": 0.04,
  "rationale": { "decision_id": "dec_01J8Z...", "policy_tier": "EXECUTE" }
}
```

**Rules**

- **No `size` field.** Size is a risk decision, made below, by rules the AI cannot
  influence. A proposal that carries its own size is a proposal that has already
  overruled the risk engine.
- `stop_pct` and `target_pct` come from `data/exit-params.json`, which is calibrated
  (existing `exit_calibrate.py`), not chosen per-trade.
- Every proposal is traceable to the `decision_id` that produced it.

---

## Contract G — Risk Decision

**Deterministic. No model, no LLM, no exceptions.**

```json
{
  "contract": "risk_decision",
  "version": "1.0",
  "proposal_id": "tp_01J8Z...",
  "approved": false,
  "checks": [
    { "name": "daily_loss_limit", "passed": false, "detail": "daily_pnl -72 vs cap -1.00" },
    { "name": "max_position_size", "passed": true, "detail": "1.2% of equity" },
    { "name": "max_leverage", "passed": true, "detail": "1x" },
    { "name": "spread_ceiling", "passed": true, "detail": "1.2 bps vs 5.0 cap" },
    { "name": "duplicate_order", "passed": true, "detail": "no matching open order" },
    { "name": "exchange_health", "passed": true, "detail": "REST 200, 91ms" },
    { "name": "kill_switch", "passed": true, "detail": "not engaged" }
  ],
  "size": null,
  "reject_reason": "daily_loss_limit"
}
```

**The veto list — these override any AI, always**

```
daily_loss_limit exceeded        → REJECT
position_size > max              → REJECT
leverage > max                   → REJECT
spread > ceiling                 → REJECT
duplicate order                  → REJECT
exchange anomaly / unreachable   → REJECT
kill switch engaged              → REJECT
```

**Rules**

- **`size` is computed here**, from equity and the per-trade risk fraction. It is never
  supplied by an upstream proposal and never accepted from a model.
- **Every check appears in `checks`, including the ones that passed.** A log that only
  records failures cannot show that the limits are being tested.
- **The risk engine can say no to a 97%-confidence decision**, and that is the entire
  point. Type safety is not trading safety.
- The existing `daily loss cap -$1.00` and `LIVE_ENABLED` gate remain in force. V2 does
  not relax them.

---

## Contract H — Execution Command

Issued only by the execution engine, only on `approved: true`.

```json
{
  "contract": "execution_command",
  "version": "1.0",
  "command_id": "ex_01J8Z...",
  "proposal_id": "tp_01J8Z...",
  "risk_decision_id": "rd_01J8Z...",
  "venue": "binance",
  "symbol": "BTCUSDT",
  "side": "BUY",
  "type": "MARKET|LIMIT",
  "size": 0.00092,
  "position_side": "LONG",
  "reduce_only": false,
  "idempotency_key": "tp_01J8Z...-v1"
}
```

**Rules**

- **`risk_decision_id` is mandatory and must reference an `approved: true` decision.**
  Execution refuses to act on a proposal that has no passing risk decision. This is the
  one check that makes the whole chain safe.
- `idempotency_key` prevents a retry from becoming a second position.
- **Binance is hedge-mode** — `position_side` is required. (Known quirk, already handled
  in `futures_executor.py`.)
- **Bybit demo is REST-only.**
- Notifications must never be able to break a trade. A failed Telegram send is logged
  and swallowed; it is not a trade failure.

---

## Contract I — Hermes Proposal

How Hermes changes the system. **Hermes produces proposals, never edits.**

```json
{
  "contract": "hermes_proposal",
  "version": "1.0",
  "proposal_id": "prop_0182",
  "type": "NEW_STRATEGY|MODIFY_STRATEGY|CHANGE_THRESHOLD|CHANGE_JEV_WORKFLOW|CHANGE_FEATURE|CHANGE_POSITION_SIZING|DISABLE_STRATEGY|ENABLE_STRATEGY",
  "target": "btc_breakout_v3",
  "change": { "entry_probability": 0.82, "regime_filter": "high_volatility_excluded" },
  "evidence": { "backtest_trades": 8421, "sharpe_delta": 0.31, "max_drawdown_delta": -0.04 },
  "status": "proposed"
}
```

**The path — no shortcuts**

```
Hermes → git branch → tests → backtest → validation → paper → promotion
```

**Never:**

```
Hermes → edit production.py → restart bot
```

**Rules**

- Proposals are inert. They activate only through the gate ladder below.
- `evidence` is mandatory. A proposal without a backtest is an opinion.
- The existing rule stands: `strategy_book.py` / `strategy_lab.py` / `strategy_compact.py`
  **propose only — never auto-apply, never trade.**

---

## Hermes approval gates

| Gate | Action | Requires |
|---|---|---|
| **0** | Read everything | nothing beyond auth |
| **1** | Create / modify code | branch, tests pass, review |
| **2** | Run backtest | gate 1 |
| **3** | Promote to paper | gate 2 + positive split-half |
| **4** | Promote to live | **explicit owner approval** |
| **5** | Runtime trading decisions | not Hermes — Jev + risk engine |
| **6** | Emergency halt | request only; deterministic controller decides |

**Hermes permissions**

| Capability | Hermes |
|---|---|
| Read market data / positions / trades | ✅ |
| Run analysis / backtests | ✅ |
| Create tools, strategies, Jev workflow | ✅ |
| Modify production strategy | 🟡 gated (gate 1–3) |
| Start paper strategy | 🟡 gated (gate 3) |
| **Enable live strategy** | ❌ |
| **Place order** | ❌ |
| **Change risk limits / leverage** | ❌ |
| **Withdraw funds** | ❌ |
| **Disable kill switch** | ❌ |
| Request `HALT_STRATEGY` | ✅ (request only) |

---

## Rollout ladder

| Stage | What runs | Risk |
|---|---|---|
| **V2.0 Shadow** | Jev decides, nothing executes, decisions logged and compared to current system | none |
| **V2.1 Paper** | Jev → policy → risk → paper book | none |
| **V2.2 Restricted live** | smallest tier, existing live gate still in force | real, capped |
| **V2.3 Autonomous research** | Hermes proposes and backtests without prompting | none (no live path) |
| **V2.4 Autonomous promotion** | Hermes promotes to paper itself; live still owner-gated | none to live |

**Shadow first is not optional.** Any disagreement between Jev and the current system
during V2.0 is free information, and it is the cheapest place to discover that the
decision schema is wrong.

---

## Implementation notes

**Do not implement on the live path without owner approval.** The existing
`config/LIVE_ENABLED` gate and the Phase-4 crontab header are authoritative.

**Use the existing deploy discipline:** branch, never `main`; tests must stay green
(currently 482 passed / 3 skipped); `git pull --ff-only` only; never force-push the
public repo; never loosen `.gitignore`; read diffs line by line, not summaries.

**Memory:** this box has ~2.4 GB available and runs `livr-shadow` (~946 MB, a 60-day
forward clock that must not be restarted), `livr-recorder` (the only un-refetchable
data), the feed, Caddy, the gateway, and 40+ cron jobs. **Any V2 component that adds
a resident process must justify its memory.** Redis and Postgres are named in the
architecture note; either would be a significant new resident cost on this box and
should be evaluated against simply continuing to use SQLite.

**What V2 does *not* change:** the risk engine's authority, the `LIVE_ENABLED` gate,
the paper/real separation, the deploy-key scoping, and the rule that paper and real
records are never blended.
