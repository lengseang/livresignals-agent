"""Live scanner: live ticks -> closed 1m bars -> the SAME strategy loop as replay (LIVE_PAPER, paper only).

Design: docs/loop/designs/T3.2.md. Nothing in this package can place an order: it imports no exchange
client, reads market data only from the market-data mirror (trading.stream.ws_client), and writes only
simulated signal records under data/local/signals/. Every record carries simulated: true.
"""
