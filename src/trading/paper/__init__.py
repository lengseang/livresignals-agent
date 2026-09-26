"""Paper execution engine with the measured fill model (T4.2; design docs/loop/designs/T4.1.md).

PAPER ONLY. Nothing here can place an order: this package imports no exchange or live client (ccxt,
trading.exchange, the real-money scripts), and a subprocess import-graph test enforces it. Every fill
it produces is labelled paper and carries the fill-model parameters it was made with.
"""
