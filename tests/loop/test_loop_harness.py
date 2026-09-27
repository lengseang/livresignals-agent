"""Smoke tests proving the tests/loop harness guards actually fire."""
from __future__ import annotations

import importlib
import socket
import sys

import pytest


def test_interpreter_matches_production():
    # Production runs CPython 3.12.3; a newer interpreter lets code through
    # that fails on the box.
    assert sys.version_info[:2] == (3, 12), sys.version


def test_src_package_importable():
    import trading  # noqa: F401  (pytest.ini puts src/ on the path)


def test_network_is_refused():
    with pytest.raises(OSError, match="network disabled"):
        socket.create_connection(("127.0.0.1", 9))


def test_raw_socket_connect_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="network disabled"):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


@pytest.mark.parametrize("name", ["trading.exchange.live_client", "live_executor", "trend_follower",
                                  "futures_executor", "lev_satellite_manage"])
def test_real_money_modules_cannot_be_imported(name):
    with pytest.raises(ImportError, match="real-money module"):
        importlib.import_module(name)
