from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import API_HOST, API_PORT

logger = logging.getLogger(__name__)

# Slack destination for notifications. There is no default: an unset value
# means "do not post to Slack", so a fresh clone never messages a stranger's
# channel. Set WAKELITE_SLACK_CHANNEL to a channel or DM id such as "C0123ABCD".
SLACK_CHANNEL = os.environ.get("WAKELITE_SLACK_CHANNEL", "")

# Distinguishes "not looked up yet" from "looked up, not installed" (None).
_UNRESOLVED = object()

# Keychain item holding the Slack bot token (ai-audit A7, 2026-06-15).
# Moved out of plaintext ~/.claude.json. Both halves are configurable so the
# lookup matches whatever you named the item:
#   security add-generic-password -s "$WAKELITE_KEYCHAIN_SERVICE" \
#       -a "$WAKELITE_KEYCHAIN_ACCOUNT" -w <token>
_KEYCHAIN_SERVICE = os.environ.get("WAKELITE_KEYCHAIN_SERVICE", "wakelite-slack-bot-token")
_KEYCHAIN_ACCOUNT = os.environ.get("WAKELITE_KEYCHAIN_ACCOUNT", "wakelite")


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
    _poster_cache: Any = _UNRESOLVED

    def __init__(self, meta_store: Any = None) -> None:
        self.muted = False
        self._daily_thread_ts_by_day: dict[str, str] = {}
        # Anything exposing get_meta/set_meta (the StateStore in practice), so
        # today's Slack thread outlives this process.
        self._meta_store = meta_store

    @staticmethod
    def ui_url(fragment: str = "") -> str:
        """A deep link into the local dashboard, for notification clicks."""
        return f"http://{API_HOST}:{API_PORT}/ui{fragment}"

    @classmethod
    def _posters(cls) -> list:
        """Notification posters that can carry a click target, best first.

        WakeLiteNotify.app is built from notifier-app/ and posts under
        WakeLite's own bundle identity, so the click opens the dashboard.
        terminal-notifier is kept as a second choice because it works on
        macOS versions that will grant it permission; on macOS 26 it is
        refused and is skipped after its first failure.
        """
        if cls._poster_cache is _UNRESOLVED:
            found = []
            app = Path.home() / "Applications/WakeLiteNotify.app/Contents/MacOS/WakeLiteNotify"
            if app.exists():
                found.append(("wakelite-notify", str(app)))
            binary = shutil.which("terminal-notifier")
            if binary:
                found.append(("terminal-notifier", binary))
            cls._poster_cache = found
        return cls._poster_cache

    @staticmethod
    def _poster_command(kind: str, path: str, title: str, message: str,
                        open_url: Optional[str], group: Optional[str]) -> list:
        if kind == "wakelite-notify":
            cmd = [path, "--title", title, "--message", message]
            if open_url:
                cmd += ["--url", open_url]
            if group:
                cmd += ["--group", group]
            return cmd
        cmd = [path, "-title", title, "-message", message]
        if open_url:
            cmd += ["-open", open_url]
        if group:
            cmd += ["-group", group]
        return cmd

    def notify(
        self,
        title: str,
        message: str,
        open_url: Optional[str] = None,
        group: Optional[str] = None,
    ) -> None:
        """Post a desktop notification, clickable where possible.

        AppleScript's `display notification` carries no click action: macOS
        attributes an osascript notification to Script Editor, so clicking one
        opens Script Editor's document picker instead of anything to do with
        WakeLite. The posters above can carry `open_url`, so the click lands on
        the timer or report the alert is about. `group` collapses repeat alerts
        for the same timer into one, so a five-minute health check that fails
        all afternoon does not bury everything else.

        Falls back to osascript when no poster is available or all of them
        fail, so the alert still arrives — just without the click target.
        """
        if self.muted:
            return

        for kind, path in list(self._posters()):
            cmd = self._poster_command(kind, path, title, message, open_url, group)
            try:
                result = subprocess.run(cmd, check=False, capture_output=True, timeout=10)
                if result.returncode == 0:
                    return
                detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:200]
            except Exception:
                detail = "raised"
                logger.debug("%s raised", kind, exc_info=True)
            # Permission denial is permanent for the life of this process, so
            # do not pay a doomed subprocess per notification. A runner restart
            # re-probes, which is how a later permission grant takes effect.
            type(self)._poster_cache = [
                entry for entry in self._posters() if entry[0] != kind
            ]
            logger.info(
                "notification poster %s failed (%s); skipping it for the rest of this process",
                kind,
                detail,
            )

        safe_message = message.replace('"', "'")
        safe_title = title.replace('"', "'")
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
            if not channel:
                logger.debug("Slack notify skipped: no channel (set WAKELITE_SLACK_CHANNEL)")
                return None
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

    def log_sent(self, kind: str, timer_id: str, streak: int = 0) -> None:
        """Record one delivered notification, so alerts can be counted.

        Delivery used to be logged only when it failed, which left no way to
        answer "how many alerts did that outage send?" from the runner log —
        the one question an alert-collapse policy has to be checked against.
        """
        logger.info("notify.sent timer=%s kind=%s streak=%d", timer_id, kind, int(streak))

    def _thread_meta_key(self, day: str) -> str:
        return f"slack.daily_thread_ts.{day}"

    def get_daily_thread_ts(self, channel: str = SLACK_CHANNEL) -> Optional[str]:
        """Return today's WakeLite Slack thread root, creating it if needed.

        Persisted per day: the id used to live only in memory, so every runner
        restart opened another "Timer activity" thread and scattered the day's
        runs across as many threads as the runner had lives.
        """
        day = datetime.now().strftime("%Y-%m-%d")
        cached = self._daily_thread_ts_by_day.get(day)
        if cached:
            return cached

        stored = None
        if self._meta_store is not None:
            try:
                stored = self._meta_store.get_meta(self._thread_meta_key(day))
            except Exception:
                logger.warning("Reading the persisted Slack thread failed", exc_info=True)
        if stored:
            self._daily_thread_ts_by_day[day] = stored
            return stored

        ts = self.notify_slack(f"Timer activity - {day}", channel=channel)
        if ts:
            self._daily_thread_ts_by_day[day] = ts
            if self._meta_store is not None:
                try:
                    self._meta_store.set_meta(self._thread_meta_key(day), ts)
                except Exception:
                    logger.warning("Persisting the Slack thread failed", exc_info=True)
        return ts

    def should_emit_morning_digest(self, now: datetime) -> bool:
        return now.hour >= 6
