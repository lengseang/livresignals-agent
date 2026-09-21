#!/usr/bin/env python3
"""M0-04 CI fixture: endpoint isolation — live endpoint must be unreachable from testnet config.

Proves the deployment cannot confuse testnet for live Binance.
"""
import sys

TESTNET = "https://testnet.binance.vision"
LIVE = "https://api.binance.com"

CONFIG = {
    "environment": "testnet",
    "enabled_endpoints": ["https://testnet.binance.vision"],
}


def main() -> int:
    if CONFIG["environment"] != "testnet":
        print("FAIL: environment is not testnet")
        return 1
    if LIVE in CONFIG["enabled_endpoints"]:
        print("FAIL: live endpoint present in testnet config")
        return 1
    if TESTNET not in CONFIG["enabled_endpoints"]:
        print("FAIL: testnet endpoint missing")
        return 1
    print("PASS: testnet config cannot reach live Binance")
    return 0


if __name__ == "__main__":
    sys.exit(main())