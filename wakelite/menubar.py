from __future__ import annotations

import json
import subprocess
import urllib.request

from .config import API_HOST, API_PORT


def fetch_health() -> dict:
    with urllib.request.urlopen(f"http://{API_HOST}:{API_PORT}/v1/health", timeout=2.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    try:
        import rumps  # type: ignore
    except Exception:
        raise SystemExit("rumps is required for menubar UI. Install with: pip install rumps")

    class WakeLiteBar(rumps.App):
        def __init__(self) -> None:
            super().__init__("WakeLite", quit_button="Quit")
            self.menu = ["Open UI", "Refresh Health", "Status: unknown"]

        @rumps.clicked("Open UI")
        def open_ui(self, _):
            subprocess.run(["open", f"http://{API_HOST}:{API_PORT}/ui"], check=False)

        @rumps.clicked("Refresh Health")
        def refresh(self, _):
            self.update_status()

        def update_status(self) -> None:
            try:
                h = fetch_health()
                self.menu["Status: unknown"].title = (
                    f"Status: {h.get('status')} | enabled={h.get('timers_enabled')} | incidents={h.get('unacked_incidents')}"
                )
            except Exception:
                self.menu["Status: unknown"].title = "Status: service unavailable"

    app = WakeLiteBar()
    app.update_status()
    app.run()


if __name__ == "__main__":
    main()
