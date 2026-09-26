"""Keyword symbol tags for the 15-coin paper universe.

A coin is tagged when its ticker appears in CAPITALS (case-sensitive, word boundary, so
"$BTC" matches and "WBTC"/"stETH" don't) or its full name appears. Unambiguous names match
in any case; names that are also ordinary words (Ether, Avalanche, Sui, Cosmos) match only
capitalised, and NEAR needs the ticker or "Near Protocol". Known false-positive risks
(ALL-CAPS headlines, "ADA" the US law, sentence-initial "Avalanche") are in
docs/loop/designs/T2.4.md. "Ripple" (the company) does not tag XRP; "Binance" does not tag BNB.
"""
from __future__ import annotations

import re

_I = re.IGNORECASE

_RULES: dict[str, list[re.Pattern]] = {
    sym: [re.compile(p, flags) for p, flags in pats]
    for sym, pats in {
        "BTC": [(r"\bBTC\b", 0), (r"\bbitcoin\b", _I)],
        "ETH": [(r"\bETH\b", 0), (r"\bethereum\b", _I), (r"\bEther\b", 0)],
        "SOL": [(r"\bSOL\b", 0), (r"\bsolana\b", _I)],
        "XRP": [(r"\bXRP\b", 0)],
        "BNB": [(r"\bBNB\b", 0), (r"\bbinance coin\b", _I)],
        "ADA": [(r"\bADA\b", 0), (r"\bcardano\b", _I)],
        "LINK": [(r"\bLINK\b", 0), (r"\bchainlink\b", _I)],
        "DOGE": [(r"\bDOGE\b", 0), (r"\bdogecoin\b", _I)],
        "AVAX": [(r"\bAVAX\b", 0), (r"\bAvalanche\b", 0)],
        "NEAR": [(r"\bNEAR\b", 0), (r"\bnear protocol\b", _I)],
        "SUI": [(r"\bSUI\b", 0), (r"\bSui\b", 0)],
        "ARB": [(r"\bARB\b", 0), (r"\barbitrum\b", _I)],
        "DOT": [(r"\bDOT\b", 0), (r"\bpolkadot\b", _I)],
        "LTC": [(r"\bLTC\b", 0), (r"\blitecoin\b", _I)],
        "ATOM": [(r"\bATOM\b", 0), (r"\bCosmos\b", 0)],
    }.items()
}

SYMBOLS = tuple(_RULES)


def tag_symbols(text: str | None) -> list[str]:
    """Sorted tickers mentioned in text."""
    if not text:
        return []
    return sorted(sym for sym, pats in _RULES.items() if any(p.search(text) for p in pats))
