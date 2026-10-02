"""
query-token-audit — Binance Web3 contract security audit wrapper
===============================================================
Endpoint: POST https://web3.binance.com/bapi/defi/v1/public/
            wallet-direct/security/token/audit
Probes:    BSC 200 OK, Solana 200 OK (verified 2026-09-18)
Status:    MONITOR-ONLY — no history, cannot backtest, labelled as unvalidated.
           See reports/overnight-2026-09-18.md for the full vetting.

Chain IDs: 56 (BSC), 8453 (Base), CT_501 (Solana), 1 (Ethereum)

Risk levels:
  0-1  LOW     → proceed with caution (NOT guaranteed safe)
  2-3  MEDIUM  → exercise caution
  4-5  HIGH    → avoid trading / block
"""

import json
import time
import logging
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

BASE_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"
TIMEOUT = 15
USER_AGENT = "binance-web3/1.4 (Skill)"

# Map Binance spot symbols to chain + contract address.
# This is the critical gap: we need contract addresses for our coins.
# Binance-listed coins exist on Ethereum (ERC-20) and/or BSC (BEP-20).
# Without a mapping, we can't call the audit endpoint by symbol alone.
#
# For now this module is a UTILITY that accepts (chainId, contractAddress)
# directly. A symbol→address resolver would need an additional data source
# (CoinGecko, CoinMarketCap, or Binance's own token info API).

def _request(chain_id: str, contract_address: str, request_id: str = None) -> dict:
    """Call the Binance Web3 token audit endpoint.
    
    Returns the full response dict (code, data, success).
    On network/parse failure returns {"success": False, "error": str(e)}.
    """
    body = json.dumps({
        "binanceChainId": chain_id,
        "contractAddress": contract_address,
        "requestId": request_id or f"audit-{int(time.time())}",
    }).encode()
    req = Request(BASE_URL, data=body,
                  headers={"Content-Type": "application/json",
                           "User-Agent": USER_AGENT,
                           "source": "agent"})
    try:
        t0 = time.time()
        with urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            ms = (time.time() - t0) * 1000
            d = json.loads(body)
        logger.debug("audit probe %.0fms chain=%s addr=%s…",
                     ms, chain_id, contract_address[:12])
        return d
    except (URLError, json.JSONDecodeError, OSError) as e:
        logger.warning("audit probe FAILED chain=%s addr=%s…: %s",
                       chain_id, contract_address[:12], e)
        return {"success": False, "error": str(e)}


def audit(chain_id: str, contract_address: str) -> dict:
    """Get security audit for a token contract.
    
    Returns a dict with normalised fields:
      success    bool   — whether the probe completed
      has_result bool   — whether audit data exists (only meaningful if success)
      supported  bool   — whether this chain/token is supported
      risk_level str    — LOW / MID / HIGH / None
      risk_code  int    — 1-5 / None
      buy_tax    str    — buy tax % / None
      sell_tax   str    — sell tax % / None
      issues     list   — human-readable issues (detected risks)
      hits       dict   — category -> [issue_title] for detected risks
      raw        dict   — the full API response
      error      str    — error message if failed
    """
    d = _request(chain_id, contract_address)
    if not d.get("success"):
        return {"success": False, "error": d.get("error", "unknown")}

    data = d.get("data") or {}
    # extraInfo may be present-and-null (native SOL returns extraInfo=None).
    # dict.get(k, {}) does NOT guard this — the default only applies when the
    # key is ABSENT. Coerce None -> {} explicitly or .get() crashes.
    extra = data.get("extraInfo") or {}
    result = {
        "success": True,
        "has_result": data.get("hasResult", False),
        "supported": data.get("isSupported", False),
        "risk_level": data.get("riskLevelEnum"),
        "risk_code": data.get("riskLevel"),
        "buy_tax": extra.get("buyTax"),
        "sell_tax": extra.get("sellTax"),
        "is_verified": extra.get("isVerified"),
        "issues": [],
        "hits": {},
        "raw": d,
    }

    # Extract detected risks into a flat list
    for ri in data.get("riskItems") or []:
        category = ri.get("id", "UNKNOWN")
        hits = [detail.get("title", "")
                for detail in ri.get("details") or []
                if detail.get("isHit")]
        if hits:
            result["hits"][category] = hits
            result["issues"].extend(hits)

    return result


def is_safe(result: dict, max_risk: str = "LOW") -> bool:
    """Check if a token passes the safety gate.
    
    Args:
        result: dict from audit()
        max_risk: maximum acceptable risk level ("LOW" only — "MID"/"HIGH" blocked)
    
    Returns:
        True if token is considered safe to trade
        False if token should be vetoed
        None if audit data is unavailable (fail-open: don't block on missing data)
    """
    if not result.get("success"):
        return None  # fail-open — network failure shouldn't block trading
    if not result.get("has_result") or not result.get("supported"):
        return None  # fail-open — can't judge unknown tokens
    level = result.get("risk_level", "HIGH")
    if max_risk == "LOW" and level in ("MID", "HIGH"):
        return False
    if max_risk == "NONE" and level == "HIGH":
        return False
    return True


def vet_symbol_tokens(symbol_chain_map: dict) -> dict:
    """Batch audit of known symbol→chain→address mapping.
    
    Args:
        symbol_chain_map: {symbol: (chain_id, contract_address)}
    
    Returns:
        {symbol: audit_result_dict}
    """
    results = {}
    for sym, (chain_id, addr) in symbol_chain_map.items():
        results[sym] = audit(chain_id, addr)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Quick sanity test
    print("=== Token Audit Module Self-Test ===\n")
    
    # BSC USDT (stablecoin — should be LOW/MID)
    print("1. BSC USDT (known safe, mintable):")
    r = audit("56", "0x55d398326f99059ff775485246999027b3197955")
    print(f"   success={r['success']} has_result={r['has_result']} "
          f"risk={r['risk_level']}({r['risk_code']}) "
          f"tax={r['buy_tax']}/{r['sell_tax']}")
    print(f"   issues: {r['issues']}")
    safe = is_safe(r)
    print(f"   is_safe(LOW): {safe}")
    print()
    
    # Solana USDC (stablecoin — should be LOW)
    print("2. Solana USDC:")
    r = audit("CT_501", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
    print(f"   success={r['success']} has_result={r['has_result']} "
          f"risk={r['risk_level']}({r['risk_code']}) "
          f"tax={r['buy_tax']}/{r['sell_tax']}")
    print(f"   issues: {r['issues']}")
    safe = is_safe(r)
    print(f"   is_safe(LOW): {safe}")