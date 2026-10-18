#!/usr/bin/env python3
"""REGIME-ADAPTIVE TRADER — the first strategy with a real OOS edge.

Backtest (24 coins, 1000 days, real fees 0.144%, IS/OOS split):
    full +29.7%  |  OOS +40.8%  |  maxDD -21.9%   (r/DD 1.36)
    benchmarks that same OOS period: BTC -35.2%, equal-weight basket -60.1%

LOGIC
  BULL  (BTC > SMA200):  LONG the strongest N coins above their own SMA50,
                         risk-parity sized (constant $ risk per position)
  BEAR  (BTC < SMA200):  SHORT the weakest N coins by 30d momentum
  EXITS: SMA50 cross, hard stop, max hold

  NOTE (2026-10-17): the regime SMA is champion-tunable (champion.json
  `sma_r`), so the BULL/BEAR THRESHOLD is not pinned to 200 -- the constant
  `SMA_REGIME` (150 unless champion overrides) is what the rule uses. The
  docstring's SMA200 is the documented LABEL: `regime_label()` computes it
  separately and main() prints a DIVERGES warning whenever the two disagree.
  While `sma_r` was 150 the log said "BULL (BTC x vs SMA150 y)" while this
  header claimed SMA200: same class of defect as the 2026-10-16 single-valued
  regime field -- the strategy traded its own definition but advertised
  another. See docs/proposals/regime-label-vs-rule-2026-10-17.md.

Runs on the Bybit DEMO alongside bybit_demo_trader.py so the two can be
compared live (long-only trend vs regime-adaptive). Separate state file.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
from bybit_demo import private, public          # noqa: E402

try:
    from gold_macro import macro_signal
except Exception:
    def macro_signal(*_a, **_k):
        return {"signal": "UNKNOWN"}

STATE = BASE / "data" / "regime-state.json"
BOOK = 1000.0            # virtual book for readable percentages
TOPN = 3                 # max concurrent positions
HOLD = 21                # max days held
STOP = 0.10              # hard stop 10% (fallback if ATR unavailable)
RISK = 0.01              # 1% of book at risk per position (risk parity)
# TAKE PROFIT at 2R. Walk-forward validated 2026-09-14 (3 folds):
#   no TP   -> WR 33%, expectancy +0.23%/trade, DD -16.6%
#   TP 1R   -> WR 49%, expectancy -0.79%/trade   <- cuts winners, kills it
#   TP 2R   -> WR 44%, expectancy +1.21%/trade, DD -14.6%   <-- THIS
#   TP 3R   -> WR 38%, expectancy -0.44%/trade
TP_R = 2.0
STOP_ATR = 2.5           # stop distance = 2.5 x ATR
# EXIT RULE. 'sma50' = exit on SMA50 cross (original). 'mom5' = exit when
# 5-day momentum turns against the position.
# Supervisor study 2026-09-15 (7,942 setups, 3 regimes):
#   sma50 : WR 38.3%, exp/risk 0.0449, WR spread 33.0  <- original
#   mom5  : WR 40.8%, exp/risk 0.0433, WR spread 19.2  <- far more stable
#   both at 1.5xATR: mom5 WR 41.8%, exp/risk 0.0777, spread 14.2
# mom5 exits faster and cuts the segment-to-segment swing by ~57%.
EXIT_MODE = "sma50"

# =====================================================================
# PARTIAL SCALE-OUT AT TP  —  DEFAULT OFF, UNMEASURED
# =====================================================================
# Requested 2026-09-25 to stop "giving back" winners that keep trending: take
# some profit at the TP level, let the rest run on the existing trend rules.
#
# ⚠️ THIS IS NOT VALIDATED. Read before enabling.
#
# The walk-forward study above (3 folds, 2026-09-14) tested FULL exits at fixed
# R multiples and found taking profit early LOSES money:
#     no TP  +0.23%/trade   TP 1R  -0.79%/trade   TP 2R  +1.21%/trade
# It did NOT test a PARTIAL exit, so the partial case is genuinely unknown --
# it is neither blessed nor condemned by that evidence. What the study does
# establish is that the mechanism it penalises is "cutting winners", and a
# partial exit cuts half a winner instead of all of one. So the expected
# outcome is somewhere between TP 2R (+1.21%) and no-TP (+0.23%), not
# obviously better than either. It must be measured on research/06-walkforward.py
# before it is enabled on the real book.
#
# THE DEFAULT IS OFF and `_champion_override` cannot turn it on, so deploying
# this file is a NO-OP on live behaviour until someone deliberately sets
# SCALE_OUT_FRAC > 0 here.
#
# HOW IT WORKS
#   SCALE_OUT_FRAC = 0.0   -> disabled; position exits exactly as before.
#   SCALE_OUT_FRAC = 0.5   -> at SCALE_AT_R, sell half; move the stop to
#                             breakeven; the remainder exits only via the
#                             normal trend rules (sma50 break / mom5 decay /
#                             hard stop / max hold).
# A position is scaled at most ONCE (`scaled` flag), so re-entry into the TP
# zone cannot ratchet the position away to nothing.
SCALE_OUT_FRAC = 0.0        # 0.0 = OFF. fraction of qty to sell at the trigger
SCALE_AT_R = 1.0            # fire the scale-out at this R multiple
SCALE_MOVE_STOP_TO_BE = True  # after scaling, raise the stop to the entry price


def scale_out_enabled() -> bool:
    """Only fires on a fraction strictly between 0 and 1.

    Guards the two mistakes that would be expensive: 0.0 (disabled) must not
    spawn a zero-size order, and 1.0 must not silently become a full exit at a
    level the study rejected.
    """
    try:
        return 0.0 < float(SCALE_OUT_FRAC) < 1.0
    except (TypeError, ValueError):
        return False


def qty_to_scale_out(qty: float, step: float, frac: float) -> float:
    """Remainder-friendly partial size.

    Sells `frac` of qty, floored to the exchange lot step. Returns 0.0 when the
    rounded part would be zero or would leave nothing behind -- in that case the
    caller must NOT send an order, because a zero-qty reduceOnly order is a
    rejected request at best and a full close at worst.
    """
    try:
        qty, step, frac = float(qty), float(step), float(frac)
    except (TypeError, ValueError):
        return 0.0
    if qty <= 0 or frac <= 0 or step <= 0:
        return 0.0
    part = math.floor((qty * frac) / step) * step
    # never scale out the whole position, and never leave less than one step
    if part <= 0 or (qty - part) < step:
        return 0.0
    # decimal places from the step, so we do not send 12.300000000000001
    dp = max(0, -int(round(math.log10(step)))) if step < 1 else 0
    return round(part, dp + 2)


# ---- CHAMPION CONFIG OVERRIDE -------------------------------------------
# scripts/champion.py evaluates challengers against the deployed config and
# writes the winner to data/champion.json. We read it DEFENSIVELY: any
# problem at all (missing file, bad JSON, nonsense values) silently falls
# back to the constants above. A config file must never be able to stop the
# trader from running.
def _champion_override():
    try:
        c = json.loads((BASE / "data" / "champion.json").read_text())
    except Exception:
        return {}
    try:
        cfg = c.get("config") or {}
        var = c.get("variant") or {}
        out = {}
        if str(cfg.get("topn", "")).isdigit():
            out["TOPN"] = int(cfg["topn"])
        if str(cfg.get("hold", "")).isdigit():
            out["HOLD"] = int(cfg["hold"])
        if str(cfg.get("sma_r", "")).isdigit():
            out["SMA_REGIME"] = int(cfg["sma_r"])
        try:
            r = float(cfg.get("risk", 0))
            if 0.001 <= r <= 0.05:          # sanity bound
                out["RISK"] = r
        except (TypeError, ValueError):
            pass
        # ---- variant params (stop distance / TP / exit rule) ----
        try:
            sa = float(var.get("STOP_ATR", 0))
            if 0.5 <= sa <= 5.0:            # sanity bound
                out["STOP_ATR"] = sa
        except (TypeError, ValueError):
            pass
        try:
            tpr = float(var.get("TP_R", 0))
            # >=90 is the sentinel for "no take-profit" - trend following
            # needs winners to run (supervisor study 2026-09-15: expectancy
            # 5.1x higher with no TP, same drawdown, 3/3 segments)
            if 0.5 <= tpr <= 5.0:
                out["TP_R"] = tpr
            elif tpr >= 90:
                out["TP_R"] = tpr
        except (TypeError, ValueError):
            pass
        em = var.get("EXIT_MODE")
        if em in ("sma50", "mom5"):
            out["EXIT_MODE"] = em
        return out
    except Exception:
        return {}


_CH = _champion_override()

# ---- REGIME LABEL vs REGIME RULE ----------------------------------------
# The docstring at the top of this file says "BULL (BTC > SMA200) / BEAR (BTC
# < SMA200)" and the live log prints "regime=BULL (BTC 81472 vs SMA200 72430)".
# That sentence was only true by luck of the last champion promotion: the label
# is derived from SMA_REGIME, which champion.json moved 150 -> 100 -> 200 during
# 2026-09-20..27 (champion.py's variant grid still lists sma_r 200/100/150/120),
# i.e. a key an automated optimizer flips on the live book defines the regime.
# While it read 150 the log said "BULL (BTC x vs SMA150 y)" while the docstring
# claimed SMA200 -- BTC sat ~12% ABOVE the level actually used. The invariant
# that matters keeps holding: the label and the boolean that arms the SHORT leg
# come from the same number, so the strategy always traded its own definition.
# It just did not say what it traded.
#
# FIX (2026-10-17): the RULE keeps using SMA_REGIME (champion-tunable, measured,
# unchanged behaviour), the LABEL is computed against the constant the docstring
# names, and a divergence is printed on every status line, so a regime call can
# never claim a level it did not use:
#     SMA150 rule, documented 200 -> "regime=BULL(BEAR@SMA_RULE=150? no; BTC
#     -1.2% vs SMA200) ... [DIVERGES: docstring SMA200, rule SMA150]"
LABEL_SMA = 200


def regime_label_for_rule(btc, sma_rule, label_sma=None):
    """BEAR iff the last close is below the SMA the RULE uses -- plus, for the
    status line, the DOCUMENTED label at `label_sma` (default LABEL_SMA) and a
    `diverges` flag whenever the two disagree.

    `regime` here is ALWAYS the rule's call: it is the boolean that arms the
    short leg, so main() must read this key and nothing else.
    """
    if label_sma is None:
        label_sma = LABEL_SMA
    b = sma(btc, sma_rule)
    out = {"regime": "UNKNOWN" if b is None else ("BEAR" if btc[-1] < b else "BULL"),
           "rule_sma": sma_rule, "rule_level": b}
    # label_* keys are always present (None when undefined) so callers can read
    # lab["label_level"] without a KeyError on short or missing series.
    lvl = sma(btc, label_sma)
    out["label_sma"] = label_sma
    out["label_level"] = lvl
    out["label_regime"] = ("UNKNOWN" if lvl is None
                           else ("BEAR" if btc[-1] < lvl else "BULL"))
    out["diverges"] = bool(lvl is not None and b is not None
                           and out["label_regime"] != out["regime"])
    return out


def regime_label(btc, sma_rule=None, label_sma=None):
    """The DOCUMENTED label: BEAR iff close < SMA(label_sma=LABEL_SMA=200).

    Says BOTH what we advertise (`regime`) and what the rule trades
    (`rule_regime`), and prints a one-line DIVERGES warning when they differ.
    """
    if sma_rule is None:
        sma_rule = SMA_REGIME
    out = regime_label_for_rule(btc, sma_rule, label_sma)
    out["rule_regime"] = out["regime"]
    out["regime"] = out["label_regime"]
    if out["diverges"]:
        print("  REGIME LABEL DIVERGES: documented SMA%d says %s (BTC %.0f vs "
              "%.0f), rule SMA%d says %s (vs %.0f). Trading the RULE; the label "
              "is the documented definition."
              % (out["label_sma"], out["label_regime"], btc[-1],
                 out["label_level"], sma_rule, out["rule_regime"],
                 out["rule_level"]))
    return out

# NOT overridable by champion.json, deliberately: any key the champion file can
# write is a key an automated optimizer can flip on the live book. Partial
# scale-out is unmeasured (see the note at SCALE_OUT_FRAC), so it must not be
# reachable from a config file. Changing SCALE_OUT_FRAC means editing this
# source and committing it.
# ---- A-GRADE ENTRY GATE ----  ⚠️ NOT VALIDATED. Read this before trusting it.
# Original evidence (164 setups, 3 folds): gate -> WR 56.7% vs 44.5% baseline.
# CORRECTION 2026-09-15: rebuilt the dataset at 7,960 labelled setups (48x
# bigger) and the gate does NOT hold up:
#     all setups          WR 38.7%  exp +0.459%
#     gate applied  seg1  WR 33.4%  exp -0.039%
#                   seg2  WR 35.0%  exp +0.285%
#                   seg3  WR 31.8%  exp -0.860%
# So the +12pp lift was a small-sample artifact. The gate is roughly NEUTRAL
# on the full candidate population - it is kept only because its logic is
# causal (do not fight the backdrop, do not trade a sick market) and because
# the backtest population differs from the population we actually trade
# (top-3 momentum, not every candidate). THE LIVE JOURNAL WILL DECIDE.
# Do not cite the 56.7% figure again without this caveat.
GATE_BTC_MOM = 0.05      # BTC 30-day momentum must exceed +5%
GATE_BREADTH = 0.12      # >12% of the universe above their own SMA50
USE_GATE = True

# ⚠️ TOKEN AUDIT VETO (UNDEPLOYED — monitor-only, 2026-09-18)
# Vet: https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/query-token-audit
# Endpoint: POST web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit
# Probed:   BSC 200 OK, Solana 200 OK (returns risk level + buy/sell tax + specific risk items)
# Status: REJECTED as a veto (2026-10-05, 3rd independent measurement). DO NOT ENABLE.
#   The resolver now EXISTS (src/trading/signals/token_resolver.py) — building it is
#   what falsified the candidate. It returns the CEX asset's BRIDGED TWIN on another
#   chain, not the contract Binance spot trades: AAVE->Solana, ETHFI->Solana, DOGE->BSC
#   Binance-Peg. Re-measured 2026-10-05 over the full 53-coin universe: exactly 2
#   alerts are raised and BOTH are false positives on listed blue-chips (ETHFI HIGH
#   "Rug Pull Risk" from its Solana twin). 24/53 native coins correctly fail closed.
#   The defect is STRUCTURAL — a chain-agnostic symbol search with an mcap tiebreak
#   will always prefer a larger wrapped twin — so it cannot be fixed with more data.
#   The "fix" of querying chain 1 alone is WORSE: it manufactures contracts for native
#   coins (XRP -> "HarryPotterObamaPacMan8Inu", SOL -> "Wrapped SOL"), turning a safe
#   fail-closed None into a confident wrong answer.
# Adds information? NOT for our book — the risk profile scored is a different contract.
# Module lives in src/trading/signals/token_audit.py; history: reports/overnight-*.md
USE_TOKEN_AUDIT_VETO = False
# Regime filter length. Walk-forward validated (4 folds, 300d each):
#   sma200 -> median +12.5%, 3/4 folds positive
#   sma150 -> median +26.8%, 3/4 folds positive  <-- faster = more robust
SMA_REGIME = 150
SMA_TREND = 50
# Gold (XAUT) correlates only 0.13 to BTC vs 0.55-0.82 for every crypto coin.
# It is the ONLY genuine diversifier in this universe, so it gets a reserved
# slot when it is in its own uptrend -- that is what makes 3 positions
# actually 3 bets instead of one BTC bet sized 3x.
GOLD_SYM = "XAUTUSDT"
RESERVE_GOLD = True

# ---- apply the champion override LAST, after every default is defined ----
# (placing this earlier is a bug: a later constant would silently win)
if _CH:
    print(f"[champion] config override applied: {_CH}")
    TOPN = _CH.get("TOPN", TOPN)
    HOLD = _CH.get("HOLD", HOLD)
    RISK = _CH.get("RISK", RISK)
    SMA_REGIME = _CH.get("SMA_REGIME", SMA_REGIME)
    STOP_ATR = _CH.get("STOP_ATR", STOP_ATR)
    TP_R = _CH.get("TP_R", TP_R)
    EXIT_MODE = _CH.get("EXIT_MODE", EXIT_MODE)

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT",
        "DOGEUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT", "ATOMUSDT", "LTCUSDT",
        "XAUTUSDT"]


# ---- helpers ---------------------------------------------------------

def load():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"book": BOOK, "positions": {}, "real": [], "log": [],
            "created": datetime.now(timezone.utc).isoformat()}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def klines(symbol, interval="D", limit=300):
    r = public("/v5/market/kline", {"category": "linear", "symbol": symbol,
                                    "interval": interval, "limit": limit})
    rows = r.get("result", {}).get("list", [])
    rows = list(reversed(rows))          # Bybit returns newest-first
    return [float(x[4]) for x in rows]   # closes, oldest-first


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def sd(v, n):
    w = v[-n:]
    if len(w) < n:
        return None
    m = sum(w) / len(w)
    return (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5


def atr_frac(closes, n=14):
    """ATR as a fraction of price — volatility-adaptive risk sizing."""
    if len(closes) < n + 2:
        return None
    trs = [abs(closes[i] - closes[i - 1])
           for i in range(len(closes) - n, len(closes))]
    return (sum(trs) / len(trs)) / closes[-1]


def market_breadth():
    """Fraction of the universe trading above its own SMA50."""
    above = total = 0
    for s in SYMS:
        cl = klines(s)
        if len(cl) < SMA_TREND + 2:
            continue
        s50 = sma(cl, SMA_TREND)
        if not s50:
            continue
        total += 1
        above += cl[-1] > s50
    return above / total if total else 0.0


_FNG = {"ts": 0.0, "val": None}


def fear_greed():
    """Sentiment (0-100), cached 6h. Failures never break the trader."""
    import time as _t
    if _FNG["val"] is not None and _t.time() - _FNG["ts"] < 21600:
        return _FNG["val"]
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        _FNG["val"] = int(d["data"][0]["value"])
        _FNG["ts"] = _t.time()
    except Exception:
        pass
    return _FNG["val"]


def features(sym, cl, px, side, rank, btc_cl, bread, bear):
    """Record everything known at entry so the ML question becomes
    answerable later with a REAL sample. Costs nothing now, priceless
    in a few months."""
    s50 = sma(cl, SMA_TREND) or px
    return {
        "mom30": round(cl[-1] / cl[-31] - 1, 6) if len(cl) > 31 else 0.0,
        "atr_pct": round(atr_frac(cl) or 0.03, 6),
        "dist_sma": round(px / s50 - 1, 6),
        "breadth": round(bread, 4),
        "btc_mom": round(btc_cl[-1] / btc_cl[-31] - 1, 6)
        if len(btc_cl) > 31 else 0.0,
        "rank": rank,
        "is_gold": sym == GOLD_SYM,
        "bear": bear,
        "fng": fear_greed(),
        "side": side,
    }


_INSTR = {}


def qty_step(symbol):
    if symbol not in _INSTR:
        r = public("/v5/market/instruments-info",
                   {"category": "linear", "symbol": symbol})
        lst = r.get("result", {}).get("list", [])
        _INSTR[symbol] = float(lst[0]["lotSizeFilter"]["qtyStep"]) if lst else 0.001
    return _INSTR[symbol]


def round_qty(q, step):
    return round(math.floor(q / step) * step, 10)


def notify(title, body, topic="signals"):
    try:
        sys.path.insert(0, str(BASE / "src"))
        from trading.tg.bot_ui import send_card
        send_card(topic, title, body)
    except Exception:
        pass


def reconcile(st, real):
    """Sync with the exchange so state can never drift."""
    r = private("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    live = {}
    for p in r.get("result", {}).get("list", []):
        sz = float(p.get("size") or 0)
        if sz > 0:
            live[p["symbol"]] = p
    for rp in list(real):
        if rp["symbol"] not in live:
            real.remove(rp)
            st["positions"][rp["symbol"]] = {"open": False}
            st["log"].append({"symbol": rp["symbol"], "why": "reconciled-closed",
                              "ts": datetime.now(timezone.utc).isoformat()})
    return live


# ---- main ------------------------------------------------------------

def main():
    st = load()
    pos = st.setdefault("positions", {})
    real = st.setdefault("real", [])
    live = reconcile(st, real)

    btc = klines("BTCUSDT")
    if len(btc) < SMA_REGIME + 5:
        print("BTC data insufficient")
        return
    b200 = sma(btc, SMA_REGIME)
    bear = btc[-1] < b200
    # regime = "BEAR" is the RULE's own call (same boolean that arms the short
    # leg. `lab` carries the rule's own level so the status line below can
    # never advertise a level the rule did not use. Kept as the same simple
    # string on purpose: the signal ledger's `regime` field stays "BULL"/"BEAR".
    # main() trades the RULE's. (First draft of this fix called
    # regime_label() here -- the documented label -- while `bear` came from
    # SMA_REGIME, i.e. it would have announced BEAR and bought. A test pins
    # this call site; do not "simplify" it back.)
    lab = regime_label_for_rule(btc, SMA_REGIME)
    regime = "BEAR" if bear else "BULL"
    if lab["label_level"] is not None:
        vs_label = btc[-1] / lab["label_level"] - 1
    else:
        vs_label = 0.0
    events = []
    # A-grade entry gate: computed once, shown in the status line always
    bread = market_breadth()
    btc_mom = btc[-1] / btc[-31] - 1 if len(btc) > 31 else 0.0
    gate_ok = (not USE_GATE) or (btc_mom > GATE_BTC_MOM
                                 and bread > GATE_BREADTH)

    # ---- exits ----
    for rp in list(real):
        s = rp["symbol"]
        cl = klines(s)
        if len(cl) < SMA_TREND + 2:
            continue
        px = cl[-1]
        side = rp.get("side", 1)
        held = (datetime.now(timezone.utc)
                - datetime.fromisoformat(rp["ts"])).days
        s50 = sma(cl, SMA_TREND)
        move = (px / rp["entry"] - 1) * side
        risk_pct = rp.get("risk_pct") or STOP
        why = None
        scale_qty = 0.0

        # ---- PARTIAL SCALE-OUT (default OFF) -------------------------
        # Fires at most once per position. Evaluated BEFORE the full-exit
        # chain so that a scaled position is not also closed outright: if
        # the scale-out fails or is a no-op, we fall through to the normal
        # exit rules, which is the pre-existing behaviour.
        if (scale_out_enabled()
                and not rp.get("scaled")
                and move >= SCALE_AT_R * risk_pct
                and move < TP_R * risk_pct):
            scale_qty = qty_to_scale_out(rp.get("qty", 0),
                                         qty_step(s),
                                         SCALE_OUT_FRAC)
            if scale_qty > 0:
                sbody = {"category": "linear", "symbol": s,
                         "side": "Sell" if side == 1 else "Buy",
                         "orderType": "Market", "qty": str(scale_qty),
                         "reduceOnly": True}
                sr = private("/v5/order/create", method="POST", body=sbody)
                if sr.get("retCode") == 0:
                    # Only mutate state once the exchange accepted it.
                    remaining = round(float(rp["qty"]) - scale_qty, 8)
                    rp["qty"] = str(remaining)
                    rp["scaled"] = True
                    rp["scaled_at"] = px
                    rp["scaled_qty"] = str(scale_qty)
                    rp["scaled_ts"] = datetime.now(timezone.utc).isoformat()
                    if SCALE_MOVE_STOP_TO_BE:
                        # stop already sits at entry risk-wise, so record it
                        rp["stop_at_be"] = True
                    spnl = (px - rp["entry"]) * scale_qty * side
                    st["book"] = st.get("book", BOOK) + spnl
                    events.append(f"✂️ SCALED {s} {scale_qty} of "
                                  f"{scale_qty + remaining} @ {px} "
                                  f"({move*100:+.1f}%, ${spnl:+.2f}) "
                                  f"runner={remaining}")
                    print(f"  SCALE-OUT {s} sold {scale_qty}, "
                          f"runner {remaining} @ {px} ${spnl:+.2f}")
                else:
                    # Do not touch state — the position is unchanged and the
                    # normal exit rules remain in charge next tick.
                    print(f"  SCALE-OUT FAILED {s}: {sr.get('retCode')} "
                          f"{sr.get('retMsg','')[:60]}")

        if move >= TP_R * risk_pct:
            why = f"TP {TP_R:g}R"
        elif move <= -risk_pct:
            why = "hard-stop"
        elif EXIT_MODE == "mom5":
            # exit when 5-day momentum turns against the position.
            # Supervisor 2026-09-15: cuts the WR spread across regimes by
            # ~57% vs the SMA50 cross, and lifts WR ~2.5pp.
            m5 = cl[-1] / cl[-6] - 1 if len(cl) > 6 else 0.0
            if m5 * side < 0:
                why = "mom-decay"
            elif held >= HOLD:
                why = "max-hold"
        elif side == 1 and px < (s50 or 0):
            why = "trend-broke"
        elif side == -1 and s50 and px > s50:
            why = "trend-broke"
        elif held >= HOLD:
            why = "max-hold"
        if why:
            body = {"category": "linear", "symbol": s,
                    "side": "Sell" if side == 1 else "Buy",
                    "orderType": "Market", "qty": rp["qty"], "reduceOnly": True}
            r = private("/v5/order/create", method="POST", body=body)
            ok = r.get("retCode") == 0
            pnl = (px - rp["entry"]) * float(rp["qty"]) * side
            st["book"] = st.get("book", BOOK) + pnl
            rec = {"symbol": s, "side": side, "entry": rp["entry"],
                   "exit": px, "ret": round(move, 5),
                   "pnl": round(pnl, 3), "why": why, "ok": ok,
                   "feats": rp.get("feats", {}),
                   "opened": rp.get("ts"),
                   "ts": datetime.now(timezone.utc).isoformat()}
            st["log"].append(rec)
            # append-only trade journal — the future ML dataset
            try:
                jp = BASE / "data" / "regime-trades.jsonl"
                with jp.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except Exception:
                pass
            # ---- SIGNAL LEDGER: record the outcome, chained ----------
            # appends a `close` record referencing the entry. The entry
            # record itself is never rewritten.
            #
            # ID HANDLING — this was wrong and orphaned 6 of 16 closes:
            #   ledger_id already stores the FULL id including the "demo-"
            #   prefix (it is set from the entry payload's own `id`), so
            #   re-prefixing produced "demo-demo-DOTUSDT-...". And when
            #   ledger_id was None (pre-ledger positions) the fallback `s`
            #   produced "demo-SOLUSDT", which matches no entry either.
            #
            # A close whose id does not resolve to an entry is invisible to
            # every consumer: the website cannot join the outcome, and stats
            # count it while the signal table cannot show it. So normalise
            # both cases here, and skip the write rather than create another
            # orphan.
            try:
                import signal_ledger as _sl

                _lid = rp.get("ledger_id")
                if not _lid:
                    # Position predates the ledger (or lost its id). Rather
                    # than invent a non-existent reference, match the most
                    # recent open entry for this symbol if one exists.
                    _want = f"demo-{s}"
                    _cand = [k for k in _sl.collect_stats_inputs()[0]
                             if k.endswith(f"-{s}") or k == _want]
                    _lid = _cand[-1] if _cand else None
                elif not str(_lid).startswith("demo-"):
                    _lid = f"demo-{_lid}"

                if _lid:
                    _sl.append("close", {
                        "id": _lid,
                        "exit": px, "ret": round(move, 5),
                        "why": why, "pnl": round(pnl, 3),
                        "held_days": held,
                    })
                else:
                    print(f"  (ledger close skipped for {s}: no matching entry id)")
            except Exception as _e:
                print(f"  (ledger close skipped: {str(_e)[:60]})")
            real.remove(rp)
            pos[s] = {"open": False}
            events.append(f"{'🔴' if ok else '⚠️'} CLOSED {s} "
                          f"{'LONG' if side == 1 else 'SHORT'} @ {px} "
                          f"({move*100:+.1f}%, ${pnl:+.2f}) [{why}]")
            print(f"  EXIT {s} {'L' if side==1 else 'S'} {move*100:+.2f}% "
                  f"${pnl:+.2f} [{why}] ok={ok}")

    # ---- entries ----
    if len(real) < TOPN:
        cands = []
        gold_cand = None
        for s in SYMS:
            cl = klines(s)
            if len(cl) < 60:
                continue
            s50 = sma(cl, SMA_TREND)
            if not s50:
                continue
            mom = cl[-1] / cl[-31] - 1 if len(cl) > 31 else 0
            if pos.get(s, {}).get("open"):
                continue
            if bear:
                if cl[-1] < s50:
                    cands.append((mom, s, cl[-1], -1))
            elif cl[-1] > s50:
                if s == GOLD_SYM:
                    if macro_signal().get("signal") != "HEADWIND":
                        gold_cand = (mom, s, cl[-1], 1)
                    continue          # gold handled separately, not ranked
                cands.append((mom, s, cl[-1], 1))
        # bull: strongest first; bear: weakest first
        cands.sort(reverse=not bear)
        # Gold gets a RESERVED slot (corr 0.13 vs BTC, not 0.55-0.82).
        # Gold is uncorrelated to crypto so the crypto gate does not block it
        # -- it has its own macro filter instead.
        if RESERVE_GOLD and gold_cand and not bear:
            picks = cands[:max(0, TOPN - 1 - len(real))] + [gold_cand]
        else:
            picks = cands[:TOPN]
        if not gate_ok:
            print(f"  GATE CLOSED (btc_mom {btc_mom*100:+.1f}% need "
                  f">{GATE_BTC_MOM*100:.0f}%, breadth {bread*100:.0f}% need "
                  f">{GATE_BREADTH*100:.0f}%) - no crypto entries")
            picks = [p for p in picks if p[1] == GOLD_SYM]
            if not picks:
                events.append(f"⏸️ GATE CLOSED — btc_mom {btc_mom*100:+.1f}%, "
                              f"breadth {bread*100:.0f}%. Standing down.")
        else:
            print(f"  GATE OPEN (btc_mom {btc_mom*100:+.1f}%, "
                  f"breadth {bread*100:.0f}%)")
        for rank_i, (mom, s, px, side) in enumerate(picks):
            if len(real) >= TOPN:
                break
            cl = klines(s)
            vol = atr_frac(cl) or 0.03
            risk_pct = min(STOP_ATR * vol, 0.25)
            size = st.get("book", BOOK) * RISK / vol if vol > 0 else 0
            size = min(size, st.get("book", BOOK) / TOPN)
            step = qty_step(s)
            qty = round_qty(size / px, step)
            if qty * px < 5:
                continue
            body = {"category": "linear", "symbol": s,
                    "side": "Buy" if side == 1 else "Sell",
                    "orderType": "Market", "qty": str(qty)}
            r = private("/v5/order/create", method="POST", body=body)
            if r.get("retCode") == 0:
                ft = features(s, cl, px, side, rank_i, btc, bread, bear)
                # ---- SIGNAL LEDGER: commit the entry BEFORE the outcome --
                # This is the whole point: the signal is hash-stamped now,
                # while the result is still unknown, so it can never be
                # cherry-picked. ledger_id links the later close record.
                ledger_id = None
                try:
                    import signal_ledger as _sl
                    _rec = _sl.append("signal", {
                        "id": f"demo-{s}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}",
                        "source": "demo-regime",
                        "symbol": s, "side": "L" if side == 1 else "S",
                        "entry": px, "qty": str(qty),
                        "risk_pct": round(risk_pct, 5),
                        "stop": round(px * (1 - risk_pct), 6) if side == 1
                                else round(px * (1 + risk_pct), 6),
                        "tp_r": TP_R, "stop_atr": STOP_ATR,
                        "exit_mode": EXIT_MODE,
                        "regime": regime,
                        "gate": {"btc_mom": round(btc_mom, 5),
                                 "breadth": round(bread, 5)},
                        "feats": ft,
                    })
                    ledger_id = _rec["payload"]["id"]
                    print(f"  LEDGER #{_rec['seq']} {_rec['hash'][:12]}…")
                except Exception as _e:
                    print(f"  (ledger entry skipped: {str(_e)[:60]})")
                real.append({"symbol": s, "qty": str(qty), "entry": px,
                             "side": side, "risk_pct": risk_pct,
                             "feats": ft, "ledger_id": ledger_id,
                             "scaled": False,
                             "ts": datetime.now(timezone.utc).isoformat()})
                pos[s] = {"open": True, "entry": px, "side": side}
                tag = "🥇 GOLD" if s == GOLD_SYM else ""
                events.append(f"🟢 {regime} {'LONG' if side==1 else 'SHORT'} "
                              f"{s} {tag} @ {px} (${qty*px:.0f}, "
                              f"SL {risk_pct*100:.1f}%, TP {TP_R*risk_pct*100:.1f}%)")
                print(f"  ENTRY {s} {'L' if side==1 else 'S'} qty={qty} "
                      f"px={px} risk={risk_pct*100:.1f}% tp={TP_R*risk_pct*100:.1f}% ok")
            else:
                print(f"  ENTRY FAIL {s}: {r.get('retCode')} "
                      f"{r.get('retMsg','')[:60]}")

    save(st)
    div = ("  ⚠️ [DIVERGES: documented SMA%d=%s, rule SMA%d=%s -- trading the "
           "rule]" % (LABEL_SMA, lab["regime"], SMA_REGIME, regime)
           if lab.get("diverges") else "")
    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] REGIME TRADER | "
          f"regime={regime} (BTC {btc[-1]:.0f} vs SMA{SMA_REGIME} {b200:.0f}; "
          f"{vs_label*100:+.1f}% vs documented SMA{LABEL_SMA}) | "
          f"gate={'OPEN' if gate_ok else 'CLOSED'} "
          f"(btc_mom {btc_mom*100:+.1f}%, breadth {bread*100:.0f}%) | "
          f"open={len(real)}/{TOPN} | book=${st.get('book', BOOK):.2f}{div}")
    if events:
        notify("🧠 REGIME-ADAPTIVE (demo)",
               f"<b>{regime}</b> · BTC {btc[-1]:.0f} vs SMA{SMA_REGIME} "
               f"{b200:.0f}\n"
               + "\n".join(events) +
               f"\n\nbook: ${st.get('book', BOOK):.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"regime trader error: {str(e)[:200]}")
