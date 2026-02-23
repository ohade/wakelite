from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SLACK_CHANNEL = "<set-WAKELITE_SLACK_CHANNEL>"


class Notifier:
    def __init__(self) -> None:
        self.muted = False

    def notify(self, title: str, message: str) -> None:
        if self.muted:
            return
        safe_message = message.replace("\"", "'")
        safe_title = title.replace("\"", "'")
        script = f'display notification "{safe_message}" with title "{safe_title}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
        except Exception:
            # Notification delivery should never crash runtime flow.
            return

    def notify_slack(self, text: str, channel: str = SLACK_CHANNEL) -> None:
        """Send a Slack DM. Not gated by self.muted — Slack is always-on.

        Never raises — logs errors and returns silently.
        """
        try:
            config_path = Path.home() / ".claude.json"
            with open(config_path) as f:
                token = json.load(f)["mcpServers"]["slack"]["env"]["SLACK_BOT_TOKEN"]
            payload = json.dumps({
                "channel": channel,
                "text": text,
                "unfurl_links": False,
            }).encode()
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=payload,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if not resp.get("ok"):
                logger.warning("Slack notify failed: %s", resp.get("error", resp))
        except Exception:
            logger.warning("Slack notify error", exc_info=True)

    def should_emit_morning_digest(self, now: datetime) -> bool:
        return now.hour >= 6
