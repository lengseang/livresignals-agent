"""Run the read-only paper feed on loopback.

    python -m trading.feed.serve --port 8765 [--data-root data/local]

Binds 127.0.0.1 by default. Only loopback addresses are accepted (127.0.0.1, ::1, localhost): the feed has no
auth because it is local and read-only, so it must never listen on a LAN or public interface.
"""
from __future__ import annotations

import argparse

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help=f"loopback only: one of {LOOPBACK}")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-root", default="data/local", help="read-only: the feed never writes here")
    ap.add_argument("--heartbeat-s", type=float, default=1.0)
    ap.add_argument("--quotes-s", type=float, default=1.0, help="quotes conflation interval (>= 1 s per T6.1)")
    ap.add_argument("--buffer-size", type=int, default=50_000)
    ap.add_argument("--candidates", default="research/candidates/candidates.json",
                    help="ROADMAP v3 candidate cards served by /v1/candidates (read-only)")
    a = ap.parse_args(argv)
    if a.host not in LOOPBACK:
        ap.error(f"--host {a.host!r} refused: the feed binds loopback only {LOOPBACK}")
    if a.quotes_s < 1.0:
        ap.error("--quotes-s must be >= 1.0 (T6.1: quotes at most 1 batch/s)")
    return a


def main(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    import uvicorn

    from trading.feed.app import create_app
    app = create_app(a.data_root, heartbeat_s=a.heartbeat_s, quotes_s=a.quotes_s, buffer_size=a.buffer_size,
                     bind=f"{a.host}:{a.port}", candidates=a.candidates)
    print(f"paper feed (SIMULATED, read-only) on http://{a.host}:{a.port} data_root={a.data_root}", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
