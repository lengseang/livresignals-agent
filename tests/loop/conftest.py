"""Test harness for code written by the autonomous loop (docs/loop/LOOP.md).

Every test under tests/loop/ runs inside two guards, applied automatically:

1. No network. Loop code is tested against recorded fixtures, never a live
   venue. A test that needs a socket must say so with @pytest.mark.network.
2. No real-money modules. Paper code must never be able to reach an order
   path, so importing any module in BLOCKED_MODULES fails the test. This is
   the executable form of the autonomy boundary, not a style preference.

These guards are scoped to tests/loop/ only; the existing suite is unaffected.
"""
from __future__ import annotations

import importlib.abc
import socket
import sys

import pytest

# Modules that place or manage real orders. Loop code must not import them,
# directly or transitively.
BLOCKED_MODULES = frozenset({
    "trading.exchange.live_client",
    "live_executor",
    "trend_follower",
    "futures_executor",
    "lev_satellite_manage",
    "unified_trader",
    "manager",
})


class _BlockRealMoney(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED_MODULES:
            raise ImportError(
                f"{fullname} is a real-money module; loop code must not import it "
                f"(see docs/loop/LOOP.md §2)"
            )
        return None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: test is allowed to open network connections"
    )


@pytest.fixture(autouse=True)
def _no_real_money_imports():
    already = BLOCKED_MODULES.intersection(sys.modules)
    if already:
        pytest.fail(f"real-money module already imported before test: {sorted(already)}")
    finder = _BlockRealMoney()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("network"):
        yield
        return

    def _refuse(*args, **kwargs):
        raise OSError("network disabled in tests/loop (mark the test with @pytest.mark.network)")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    yield
