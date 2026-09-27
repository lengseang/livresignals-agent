"""Historical replay: history bar-by-bar through the same strategy code as live (REPLAY, paper only).

Design: docs/loop/designs/T3.0-T3.1.md. Nothing in this package can place an order: it imports no
exchange client, and its output is written only under data/local/replay/, never to the real ledger.
Replay results are research, not evidence of edge.
"""
