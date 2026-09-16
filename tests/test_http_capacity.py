"""HTTP-level contract test for CapacityExceededError → 409 + CAPACITY_EXCEEDED.

WL-12 preserves this public contract. A refactor that renames the exception
or drops the 409 mapping would silently break the web UI,
neither of which would be caught by service-layer tests.
"""
from __future__ import annotations

import importlib
import json
import os
import socket
import tempfile
import unittest
import urllib.request
import urllib.error


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _bootstrap(temp_home: str):
    os.environ["WAKELITE_HOME"] = temp_home
    import wakelite.config as config
    import wakelite.service as service
    import wakelite.state as state
    import wakelite.timer_store as timer_store
    import wakelite.http_api as http_api

    importlib.reload(config)
    importlib.reload(state)
    importlib.reload(timer_store)
    importlib.reload(service)
    importlib.reload(http_api)

    return service.WakeLiteService, http_api.ApiServer


class HttpCapacityContractTests(unittest.TestCase):
    def test_create_timer_over_capacity_returns_409_with_code(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService, ApiServer = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=1)

            # Pre-populate a daemon consuming the single slot.
            svc.timer_store.create_timer({
                "name": "hog",
                "comment": "Consumes the only slot",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 999"},
            })

            port = _free_port()
            socket_path = tempfile.mkdtemp() + "/wl.sock"
            from pathlib import Path
            server = ApiServer(svc, host="127.0.0.1", port=port, socket_path=Path(socket_path))
            server.start()
            try:
                body = json.dumps({
                    "idempotency_key": "http-cap-test",
                    "name": "one-too-many",
                    "comment": "Should be blocked",
                    "enabled": True,
                    "timer_type": "daemon",
                    "recurrence": {"frequency": "interval", "every": "0s"},
                    "command": {"mode": "shell", "shell": "sleep 999"},
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/timers",
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)

                self.assertEqual(ctx.exception.code, 409, "expected HTTP 409 for capacity violation")
                resp_body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertEqual(resp_body.get("code"), "CAPACITY_EXCEEDED")
                self.assertIn("error", resp_body)
                self.assertIn("_executor.slot", resp_body["error"])
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
