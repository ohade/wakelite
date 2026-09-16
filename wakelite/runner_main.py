from __future__ import annotations

import argparse
import signal
import threading
import time

from .http_api import ApiServer
from .service import WakeLiteService


def main() -> None:
    parser = argparse.ArgumentParser(description="WakeLite runner")
    parser.add_argument("--tick-seconds", type=int, default=15)
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

    while not stop.is_set():
        time.sleep(0.5)

    api.stop()
    service.stop()


if __name__ == "__main__":
    main()
