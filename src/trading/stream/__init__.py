"""Per-second market data from Binance's market-data-only mirror (paper use only).

Design: docs/loop/designs/T2.1.md. Nothing in this package can place an order:
the host has no trading endpoints and no exchange client is imported.
"""
