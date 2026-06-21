from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_CHANNEL = "<set-WAKELITE_SLACK_CHANNEL>"

# Keychain item holding the Slack bot token (ai-audit A7, 2026-06-15).
# Moved out of plaintext ~/.claude.json. Read with:
#   security find-generic-password -s wakelite-slack-bot-token -a wakelite -w
_KEYCHAIN_SERVICE = "wakelite-slack-bot-token"
_KEYCHAIN_ACCOUNT = "wakelite"


def _resolve_slack_token() -> Optional[str]:
    """Resolve the Slack bot token. Order: env var -> macOS Keychain -> legacy ~/.claude.json."""
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if tok:
        return tok
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    try:  # legacy fallback for machines that still keep the token in the MCP config
        with open(Path.home() / ".claude.json") as f:
            return json.load(f)["mcpServers"]["slack"]["env"]["SLACK_BOT_TOKEN"]
    except Exception:
        return None


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
            token = _resolve_slack_token()
            if not token:
                logger.warning("Slack notify skipped: no token (Keychain/env/claude.json all empty)")
                return None
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
