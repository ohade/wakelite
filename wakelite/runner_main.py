from __future__ import annotations

import argparse
import signal
import threading
import time

from .http_api import ApiServer
from .mcp_server import run_http as run_mcp_http
from .service import WakeLiteService


def main() -> None:
    parser = argparse.ArgumentParser(description="WakeLite runner")
    parser.add_argument("--tick-seconds", type=int, default=15)
    parser.add_argument("--with-mcp-http", action="store_true", help="also expose MCP HTTP endpoint")
    args = parser.parse_args()

    service = WakeLiteService(tick_seconds=args.tick_seconds)
    api = ApiServer(service)

    stop = threading.Event()

    def _handle_signal(signum, frame):  # type: ignore[no-untyped-def]
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    service.start()
    api.start()

    mcp_thread = None
    if args.with_mcp_http:
        mcp_thread = threading.Thread(target=run_mcp_http, args=("127.0.0.1", 17342), daemon=True)
        mcp_thread.start()

    while not stop.is_set():
        time.sleep(0.5)

    api.stop()
    service.stop()


if __name__ == "__main__":
    main()
