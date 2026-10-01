"""
token_resolver.py — Binance spot symbol -> on-chain contract address.
====================================================================
Unblocks the query-token-audit veto (USE_TOKEN_AUDIT_VETO in regime_trader.py),
which was disabled because the audit endpoint needs (chainId, contractAddress),
not Binance spot tickers like ARBUSDT.

Method (all public, no key, no npm):
  1. Official name for the symbol from Binance's marketing symbol list
     (https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list),
     so the resolver knows e.g. OP -> "Optimism", not "opcode.so".
  2. Keyword search on Binance Web3 token search (v5, search/ai).
  3. Keep only EXACT symbol matches on a chain the audit endpoint supports
     (1 Ethereum, 56 BSC, 8453 Base, CT_501 Solana).
  4. Disambiguate by NAME SIMILARITY to the official name (>= MIN_SIM).
     Without this step BTC resolves to "Buy The Cat" on Solana — verified.
  5. Among surviving candidates pick the highest market cap.

Verified 2026-09-30: ARB->Ethereum 0xb507..., DOGE->BSC 0xba2a..., SOL->Solana
So111...; BTC correctly returns None (native BTC has no contract -> fails closed).

Status: MONITOR-ONLY helper. No history exists for on-chain audit risk, so it
cannot be backtested against the trade journal.
"""

import difflib
import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

SEARCH_URL = ("https://web3.binance.com/bapi/defi/v5/public/wallet-direct/"
              "buw/wallet/market/token/search/ai")
NAME_URL = ("https://www.binance.com/bapi/composite/v1/public/marketing/"
            "symbol/list")
AUDIT_CHAINS = {"1", "56", "8453", "CT_501"}   # Ethereum, BSC, Base, Solana
MIN_SIM = 0.85          # strict: 0.64 matched OP->"Optimus Primal" (bad)
MIN_MCAP = 5_000_000    # reject impostor micro-caps ($5k memecoins named "Sui")
TIMEOUT = 20
HDRS = {
    "source": "agent",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.0 (Skill)",
    "Accept": "application/json",
}

_name_cache = None


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or HDRS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def official_names() -> dict:
    """{SYMBOL: official full name} from Binance's public symbol list."""
    global _name_cache
    if _name_cache is not None:
        return _name_cache
    try:
        data = _get(NAME_URL, headers={"User-Agent": "Mozilla/5.0"})["data"]
        _name_cache = {e["name"]: (e.get("fullName") or e.get("localFullName")
                                   or e["name"]) for e in data if e.get("name")}
    except Exception as e:                                  # noqa: BLE001
        logger.warning("official_names fetch failed: %s", e)
        _name_cache = {}
    return _name_cache


def _search(keyword: str) -> list:
    url = f"{SEARCH_URL}?keyword={urllib.parse.quote(str(keyword))}&chainIds=1,56,8453,CT_501"
    return _get(url).get("data") or []


def _similarity(hit_name: str, official: str) -> float:
    a, b = (hit_name or "").lower(), (official or "").lower()
    if not a or not b:
        return 0.0
    # Require a genuine name match. A naive bidirectional substring test is a
    # trap: SUI/APT are native non-EVM chains the audit endpoint does NOT
    # support, so a search returns thousands of Solana memecoin impostors whose
    # names merely *contain* "sui"/"aptos" ("Sui  Robot Dog Companion",
    # "Autism Powered Trenches"). Substring-matching them scored 1.0 and made
    # the resolver audit a $5k impostor and report the blue-chip as HIGH risk.
    # Exact-or-near-exact only.
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def resolve(base_or_symbol: str, min_sim: float = MIN_SIM):
    """Resolve a Binance spot base asset (e.g. 'ARB' from 'ARB/USDT') to
    (chain_id, contract_address, hit_name, market_cap) or None.

    Returns None (fails closed) when no exact-symbol, name-validated match on
    a supported chain exists — callers must treat None as 'cannot audit',
    never as 'safe'.
    """
    sym = base_or_symbol.split("/")[0].upper()
    official = official_names().get(sym, sym)
    try:
        hits = _search(sym)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("search failed for %s: %s", sym, e)
        return None

    best = None
    for h in hits:
        if (h.get("symbol", "").upper() != sym
                or h.get("chainId") not in AUDIT_CHAINS):
            continue
        if _similarity(h.get("name"), official) < min_sim:
            continue
        mc = float(h.get("marketCap") or 0)
        if mc < MIN_MCAP:
            continue        # impostor micro-cap, not the real asset
        if best is None or mc > best[3]:
            best = (h["chainId"], h["contractAddress"], h.get("name"), mc)
    return best


if __name__ == "__main__":
    import sys
    coins = sys.argv[1:] or ["ARB", "NEAR", "DOGE", "SOL", "BTC", "OP", "ETH"]
    for c in coins:
        try:
            print(f"{c:6} -> {resolve(c)}")
        except Exception as e:                              # noqa: BLE001
            print(f"{c:6} ERR {type(e).__name__}: {str(e)[:60]}")
        time.sleep(0.3)
