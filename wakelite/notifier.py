from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_CHANNEL = "<set-WAKELITE_SLACK_CHANNEL>"


class Notifier:
    def __init__(self) -> None:
        self.muted = False
        self._daily_thread_ts_by_day: dict[str, str] = {}

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

    def notify_slack(
        self,
        text: str,
        channel: str = SLACK_CHANNEL,
        thread_ts: Optional[str] = None,
    ) -> Optional[str]:
        """Send a Slack DM. Not gated by self.muted — Slack is always-on.

        Never raises — logs errors and returns silently.
        All messages get a branded WakeLite header automatically.
        """
        branded = f":zap: *WakeLite*\n───\n{text}"
        try:
            config_path = Path.home() / ".claude.json"
            with open(config_path) as f:
                token = json.load(f)["mcpServers"]["slack"]["env"]["SLACK_BOT_TOKEN"]
            payload_data = {
                "channel": channel,
                "text": branded,
                "unfurl_links": False,
            }
            if thread_ts:
                payload_data["thread_ts"] = thread_ts
            payload = json.dumps(payload_data).encode()
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
                return None
            return resp.get("ts")
        except Exception:
            logger.warning("Slack notify error", exc_info=True)
            return None

    def get_daily_thread_ts(self, channel: str = SLACK_CHANNEL) -> Optional[str]:
        """Return today's WakeLite Slack thread root, creating it if needed."""
        day = datetime.now().strftime("%Y-%m-%d")
        cached = self._daily_thread_ts_by_day.get(day)
        if cached:
            return cached

        ts = self.notify_slack(f"Timer activity - {day}", channel=channel)
        if ts:
            self._daily_thread_ts_by_day[day] = ts
        return ts

    def should_emit_morning_digest(self, now: datetime) -> bool:
        return now.hour >= 6
