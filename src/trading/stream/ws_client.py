"""WebSocket connection to Binance's market-data-only mirror.

The host is a constant on purpose: this mirror carries market data only and has
no order or account endpoints, so nothing built on this client can trade.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import aiohttp

from trading.stream.normalize import parse

HOST = "data-stream.binance.vision"


def build_url(symbols: list[str], kinds: list[str]) -> str:
    streams = "/".join(f"{s.lower()}@{k}" for s in symbols for k in kinds)
    return f"wss://{HOST}/stream?streams={streams}"


async def messages(url: str, stall_s: float = 30.0, max_age_s: float = 23 * 3600) -> AsyncIterator[tuple[str, int]]:
    """Yield (raw_text, ts_recv_ns) from one connection.

    Returns (so the caller reconnects) on a stall longer than `stall_s`, or once the
    connection is `max_age_s` old; the server cuts connections at 24h.
    """
    opened = time.monotonic()
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, autoping=True, heartbeat=None) as ws:
            while time.monotonic() - opened < max_age_s:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=stall_s)
                except asyncio.TimeoutError:
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    yield msg.data, time.time_ns()
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                    return


async def sample(symbols: list[str], kinds: list[str], seconds: float) -> set[str]:
    """Connect briefly and return which tick kinds arrived (live smoke test)."""
    seen: set[str] = set()
    deadline = time.monotonic() + seconds
    async for raw, ts in messages(build_url(symbols, kinds)):
        t = parse(raw, ts)
        if t:
            seen.add(t.kind)
        if time.monotonic() > deadline:
            break
    return seen
