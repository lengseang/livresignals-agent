"""token_resolver tests — the guards that keep the audit from auditing impostors.

These are the adversarial cases found live on 2026-09-30: a naive resolver
misresolved native coins to Solana memecoin impostors whose name merely
contained the ticker ("Buy The Cat" for BTC, "Sui  Robot Dog Companion" for
SUI). Every test here encodes a way that happened, so a regression fails loudly.
No network: _search and official_names are monkeypatched.
"""
from __future__ import annotations

from trading.signals import token_resolver as tr


def _hit(symbol, name, chain, addr, mcap):
    return {"symbol": symbol, "name": name, "chainId": chain,
            "contractAddress": addr, "marketCap": str(mcap)}


def _patch(monkeypatch, hits, names=None):
    monkeypatch.setattr(tr, "_search", lambda kw: hits)
    monkeypatch.setattr(tr, "official_names", lambda: names or {})
    monkeypatch.setattr(tr, "_name_cache", {}, raising=False)


def test_resolves_real_match(monkeypatch):
    _patch(monkeypatch,
           [_hit("ARB", "Arbitrum", "1", "0xarb", 48e6)],
           {"ARB": "Arbitrum"})
    assert tr.resolve("ARB/USDT") == ("1", "0xarb", "Arbitrum", 48e6)


def test_fails_closed_when_no_exact_symbol(monkeypatch):
    # BTC has no contract on a supported chain; only impostors share the ticker.
    _patch(monkeypatch,
           [_hit("BTC", "Buy The Cat", "CT_501", "0xcat", 1.9e6)],
           {"BTC": "Bitcoin"})
    assert tr.resolve("BTC/USDT") is None


def test_rejects_impostor_below_mcap_floor(monkeypatch):
    # Right ticker, name contains the official name — but a $5k memecoin.
    _patch(monkeypatch,
           [_hit("SUI", "Sui Dog Companion", "CT_501", "0xdog", 4_955)],
           {"SUI": "Sui"})
    assert tr.resolve("SUI/USDT") is None


def test_rejects_fuzzy_name_match(monkeypatch):
    # OP -> "Optimus Primal" scored 0.64: close enough to trick a loose matcher.
    _patch(monkeypatch,
           [_hit("OP", "Optimus Primal", "CT_501", "0xoptim", 32_920)],
           {"OP": "Optimism"})
    assert tr.resolve("OP/USDT") is None


def test_wrong_chain_is_skipped(monkeypatch):
    _patch(monkeypatch,
           [_hit("ARB", "Arbitrum", "137", "0xpoly", 48e6)],  # Polygon: unsupported
           {"ARB": "Arbitrum"})
    assert tr.resolve("ARB/USDT") is None


def test_picks_highest_mcap_among_valid(monkeypatch):
    _patch(monkeypatch,
           [_hit("NEAR", "NEAR Protocol", "56", "0xsmall", 7e6),
            _hit("NEAR", "NEAR Protocol", "1", "0xbig", 70e6)],
           {"NEAR": "NEAR Protocol"})
    got = tr.resolve("NEAR/USDT")
    assert got is not None and got[1] == "0xbig"


def test_search_failure_fails_closed(monkeypatch):
    def boom(_kw):
        raise OSError("network down")
    monkeypatch.setattr(tr, "_search", boom)
    monkeypatch.setattr(tr, "official_names", lambda: {"SOL": "Solana"})
    assert tr.resolve("SOL/USDT") is None


def test_bare_symbol_accepted(monkeypatch):
    _patch(monkeypatch, [_hit("LINK", "Chainlink", "1", "0xlink", 14e9)],
           {"LINK": "Chainlink"})
    got = tr.resolve("LINK")
    assert got is not None and got[1] == "0xlink"
