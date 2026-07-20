from __future__ import annotations

import json
import os
import socketserver
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import API_HOST, API_PORT, SOCKET_PATH, auto_capture_terminal
from .service import CapacityExceededError, IdempotencyConflictError, WakeLiteService


class ApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiHandler(BaseHTTPRequestHandler):
    service: WakeLiteService = None  # type: ignore[assignment]

    server_version = "WakeLiteHTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep stdout clean; runner logs through file logger.
        return

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "invalid JSON body")

    def _require_idempotency(self, body: Dict[str, Any]) -> str:
        key = body.get("idempotency_key")
        if not key or not isinstance(key, str):
            raise ApiError(400, "idempotency_key is required for mutating operations")
        return key

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        try:
            if method == "GET" and path == "/":
                self._send_html(
                    """
                    <html><head><title>WakeLite</title><link rel="icon" href="/favicon.ico" type="image/svg+xml"></head>
                    <body>
                    <h1>WakeLite API</h1>
                    <p>Use <code>/v1/health</code>, <code>/v1/timers</code>, <code>/v1/runs</code>.</p>
                    </body></html>
                    """.strip()
                )
                return

            if method == "GET" and path == "/ui":
                ui_path = Path(__file__).parent.parent / "docs" / "ui2-live.html"
                if ui_path.exists():
                    self._send_html(ui_path.read_text(encoding="utf-8"))
                    return
                # Fallback: inline legacy UI
                self._send_html(
                    """
                    <html>
                    <head>
                      <title>WakeLite UI</title>
                      <link rel="icon" href="/favicon.ico" type="image/svg+xml">
                      <link rel="manifest" href="/manifest.json">
                      <link rel="apple-touch-icon" href="/apple-touch-icon.png">
                      <meta name="theme-color" content="#000000">
                      <meta name="apple-mobile-web-app-capable" content="yes">
                      <meta name="apple-mobile-web-app-title" content="WakeLite">
                      <style>
                        body { font-family: -apple-system, sans-serif; margin: 24px; background: #000000; color: #E0E7FF; }
                        h1 { margin-bottom: 16px; display: flex; justify-content: center; }
                        h1 img { width: 45vw; max-width: 500px; border-radius: 18px; }
                        h2 { margin-bottom: 8px; color: #C7D2FE; }
                        h3 { color: #A5B4FC; }
                        .card { background: #0F172A; border-radius: 10px; padding: 16px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.3); border: 1px solid #1E1B4B; }
                        table { width: 100%; border-collapse: collapse; font-size: 13px; }
                        th { border-bottom: 1px solid #312E81; text-align: left; padding: 6px; color: #A5B4FC; }
                        td { border-bottom: 1px solid #1E1B4B; text-align: left; padding: 6px; color: #C7D2FE; }
                        form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; align-items: end; }
                        label { display: block; font-size: 12px; color: #818CF8; margin-bottom: 4px; }
                        input[type="text"], input[type="time"], input[type="date"], input[type="number"], select { width: 100%; box-sizing: border-box; padding: 7px 8px; border: 1px solid #312E81; border-radius: 6px; background: #1E1B4B; color: #E0E7FF; }
                        input:focus, select:focus { outline: none; border-color: #6366F1; box-shadow: 0 0 0 2px rgba(99,102,241,0.3); }
                        .check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #C7D2FE; }
                        button { background: #6366F1; color: white; border: 0; border-radius: 6px; padding: 8px 12px; cursor: pointer; font-weight: 500; }
                        button:hover { background: #818CF8; }
                        button.secondary { background: #312E81; }
                        button.secondary:hover { background: #3730A3; }
                        button.danger { background: #b91c1c; }
                        button.danger:hover { background: #991b1b; }
                        .row-delete-btn { background: #b91c1c; color: #fff; border: none; border-radius: 4px; width: 22px; height: 22px; font-size: 14px; line-height: 1; cursor: pointer; padding: 0; }
                        .row-delete-btn:hover { background: #991b1b; }
                        .full { grid-column: 1 / -1; }
                        .status { font-size: 12px; white-space: pre-wrap; color: #C7D2FE; }
                        .group { padding: 10px; border: 1px solid #312E81; border-radius: 8px; background: #1E1B4B; }
                        .hidden { display: none !important; }
                        .timer-row-clickable { cursor: pointer; }
                        .timer-row-clickable:hover { background: #1E1B4B; }
                        tr.run-success { background: rgba(16,185,129,0.1); }
                        tr.run-failed { background: rgba(239,68,68,0.1); }
                        tr.run-running { background: rgba(251,191,36,0.1); }
                        tr.run-aborted { background: rgba(251,191,36,0.08); }
                        tr.run-waiting { background: rgba(96,165,250,0.1); }
                        .logs-link { font-size: 12px; padding: 4px 8px; }
                        .run-live {
                          display: flex;
                          align-items: center;
                          gap: 8px;
                          padding: 6px 8px;
                          margin-bottom: 8px;
                          border-radius: 8px;
                          background: rgba(251,191,36,0.1);
                          color: #FBBF24;
                          font-size: 12px;
                        }
                        .run-live-dot {
                          width: 8px;
                          height: 8px;
                          border-radius: 999px;
                          background: #FBBF24;
                          animation: runPulse 1.2s ease-in-out infinite;
                        }
                        @keyframes runPulse {
                          0% { opacity: 0.3; }
                          50% { opacity: 1; }
                          100% { opacity: 0.3; }
                        }

                        .modal-backdrop {
                          position: fixed;
                          inset: 0;
                          background: rgba(2, 6, 23, 0.8);
                          display: flex;
                          align-items: center;
                          justify-content: center;
                          z-index: 9999;
                        }
                        .modal-panel {
                          width: min(1100px, 96vw);
                          max-height: 90vh;
                          overflow: auto;
                          background: #0F172A;
                          border: 1px solid #312E81;
                          border-radius: 12px;
                          padding: 14px;
                          box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
                        }
                        .modal-header {
                          display: flex;
                          justify-content: space-between;
                          align-items: center;
                          margin-bottom: 8px;
                        }
                        .modal-actions {
                          display: flex;
                          gap: 8px;
                          margin-bottom: 10px;
                        }
                        .grid-two {
                          display: grid;
                          grid-template-columns: 1fr 1fr;
                          gap: 12px;
                        }
                        .mono {
                          font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                          font-size: 12px;
                          white-space: pre-wrap;
                          background: #1E1B4B;
                          border: 1px solid #312E81;
                          border-radius: 8px;
                          padding: 8px;
                          min-height: 80px;
                          color: #C7D2FE;
                        }
                        .small-note { font-size: 12px; color: #818CF8; }
                        pre { color: #A5B4FC; }
                        a { color: #818CF8; }
                        a:hover { color: #A5B4FC; }
                        ::selection { background: #4338CA; color: #E0E7FF; }
                        input[type="checkbox"] { accent-color: #6366F1; }
                        .toggle-btn {
                          padding: 3px 10px;
                          border: 0;
                          border-radius: 4px;
                          font-size: 12px;
                          font-weight: 600;
                          cursor: pointer;
                          min-width: 50px;
                        }
                        .toggle-on { background: #065F46; color: #6EE7B7; }
                        .toggle-on:hover { background: #047857; }
                        .toggle-off { background: #7F1D1D; color: #FCA5A5; }
                        .toggle-off:hover { background: #991B1B; }
                      </style>
                    </head>
                    <body>
                      <h1><img src="/logo.png" alt="WakeLite"></h1>
                      <div class="card">
                        <h2>Add Wakeup</h2>
                        <form id="createForm">
                          <div>
                            <label for="name">Name</label>
                            <input id="name" type="text" value="daily-wakeup" required />
                          </div>
                          <div>
                            <label for="time">Run Time</label>
                            <input id="time" type="time" value="01:55" required />
                          </div>
                          <div>
                            <label for="frequency">Repeat</label>
                            <select id="frequency">
                              <option value="daily" selected>Daily</option>
                              <option value="weekly">Weekly</option>
                              <option value="monthly">Monthly</option>
                              <option value="once">One-Off</option>
                              <option value="interval">Interval</option>
                            </select>
                          </div>
                          <div>
                            <label for="lead">Wake Lead (minutes)</label>
                            <input id="lead" type="number" min="0" max="240" value="2" />
                          </div>
                          <div>
                            <label for="cwd">Working Directory</label>
                            <input id="cwd" type="text" value="~/git" />
                          </div>
                          <div class="full">
                            <label for="shell">Shell Command (`:` means wake-only no-op)</label>
                            <input id="shell" type="text" value=":" />
                          </div>
                          <div id="weeklyGroup" class="full group hidden">
                            <label>Weekly Days</label>
                            <div style="display:flex; gap:10px; flex-wrap:wrap;">
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Mon" checked />Mon</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Tue" />Tue</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Wed" />Wed</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Thu" />Thu</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Fri" />Fri</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Sat" />Sat</label>
                              <label class="check"><input type="checkbox" class="weeklyDay" value="Sun" />Sun</label>
                            </div>
                          </div>
                          <div id="monthlyGroup" class="full group hidden">
                            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
                              <div>
                                <label for="monthlyMode">Monthly Mode</label>
                                <select id="monthlyMode">
                                  <option value="day_of_month" selected>Day of Month</option>
                                  <option value="nth_weekday">Nth Weekday</option>
                                </select>
                              </div>
                              <div id="monthlyDayWrap">
                                <label for="monthlyDay">Day of Month (1-31)</label>
                                <input id="monthlyDay" type="number" min="1" max="31" value="1" />
                              </div>
                              <div id="monthlyNthWrap" class="hidden">
                                <label for="monthlyNth">Week Position</label>
                                <select id="monthlyNth">
                                  <option value="1">First</option>
                                  <option value="2">Second</option>
                                  <option value="3">Third</option>
                                  <option value="4">Fourth</option>
                                  <option value="-1">Last</option>
                                </select>
                              </div>
                              <div id="monthlyWeekdayWrap" class="hidden">
                                <label for="monthlyWeekday">Weekday</label>
                                <select id="monthlyWeekday">
                                  <option value="Mon">Monday</option>
                                  <option value="Tue">Tuesday</option>
                                  <option value="Wed">Wednesday</option>
                                  <option value="Thu">Thursday</option>
                                  <option value="Fri">Friday</option>
                                  <option value="Sat">Saturday</option>
                                  <option value="Sun">Sunday</option>
                                </select>
                              </div>
                            </div>
                          </div>
                          <div id="onceGroup" class="full group hidden">
                            <div>
                              <label for="onceDate">One-Off Date</label>
                              <input id="onceDate" type="date" />
                            </div>
                          </div>
                          <div id="intervalGroup" class="full group hidden">
                            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
                              <div>
                                <label for="intervalEvery">Every (e.g. 10s, 5m, 2h)</label>
                                <input id="intervalEvery" type="text" value="30s" />
                              </div>
                              <div>
                                <label for="timerType">Timer Type</label>
                                <select id="timerType">
                                  <option value="scheduled" selected>Scheduled</option>
                                  <option value="daemon">Daemon</option>
                                </select>
                              </div>
                              <div>
                                <label for="overlapPolicy">Overlap Policy</label>
                                <select id="overlapPolicy">
                                  <option value="skip" selected>Skip</option>
                                  <option value="queue">Queue</option>
                                  <option value="allow">Allow</option>
                                </select>
                              </div>
                            </div>
                          </div>
                          <label class="check"><input id="enabled" type="checkbox" checked />Timer Enabled</label>
                          <label class="check"><input id="wakeEnabled" type="checkbox" checked />Program Wake</label>
                          <label class="check"><input id="notifySuccess" type="checkbox" />Notify On Success</label>
                          <label class="check"><input id="notifyFailure" type="checkbox" checked />Notify On Failure</label>
                          <label class="check"><input id="slackActivity" type="checkbox" checked />Post Slack Activity</label>
                          <div><button type="submit">Create Wakeup</button></div>
                        </form>
                        <pre class="status" id="createStatus"></pre>
                      </div>
                      <div class="card">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                          <h2>Health</h2>
                          <div style="display:flex; gap:8px; align-items:center;">
                            <span class="small-note">Notifications:</span>
                            <button id="muteToggle" class="toggle-btn toggle-on">ON</button>
                          </div>
                        </div>
                        <pre id="health">loading...</pre>
                      </div>
                      <div class="card">
                        <h2>Timers</h2>
                        <table id="timers"><thead><tr><th>Name</th><th>Type</th><th>Repeat</th><th>Enabled</th><th>Resources</th><th>Next Run</th><th></th></tr></thead><tbody></tbody></table>
                        <div class="small-note">Tip: click a timer row to open details, run-now test, edit, and logs.</div>
                      </div>
                      <div class="card">
                        <h2>Recent Runs</h2>
                        <table id="runs"><thead><tr><th>Timer</th><th>Status</th><th>Scheduled</th><th>Message</th><th>Logs</th></tr></thead><tbody></tbody></table>
                      </div>

                      <div id="timerModal" class="modal-backdrop hidden">
                        <div class="modal-panel">
                          <div class="modal-header">
                            <h2 id="modalTitle">Timer Details</h2>
                            <button id="modalClose" class="secondary">Close</button>
                          </div>
                          <div class="modal-actions">
                            <button id="modalRunNow">Run Now</button>
                            <button id="modalAbort" class="danger hidden">Abort</button>
                            <button id="modalDelete" class="danger hidden">Delete</button>
                            <button id="modalEdit" class="secondary">Edit</button>
                            <button id="modalSave" class="hidden">Save</button>
                            <button id="modalCancel" class="secondary hidden">Cancel</button>
                          </div>
                          <pre class="status" id="modalStatus"></pre>

                          <div id="modalReadonlyWrap">
                            <h3>Timer (Read-Only)</h3>
                            <pre id="modalReadonly" class="mono"></pre>
                          </div>

                          <div id="modalEditWrap" class="hidden">
                            <h3>Edit Timer</h3>
                            <form id="modalEditForm">
                              <div>
                                <label for="editName">Name</label>
                                <input id="editName" type="text" required />
                              </div>
                              <div>
                                <label for="editTime">Run Time</label>
                                <input id="editTime" type="time" required />
                              </div>
                              <div>
                                <label for="editFrequency">Repeat</label>
                                <select id="editFrequency">
                                  <option value="daily">Daily</option>
                                  <option value="weekly">Weekly</option>
                                  <option value="monthly">Monthly</option>
                                  <option value="once">One-Off</option>
                                  <option value="interval">Interval</option>
                                </select>
                              </div>
                              <div>
                                <label for="editTimezone">Timezone</label>
                                <input id="editTimezone" type="text" value="local" />
                              </div>
                              <div class="full" id="editWeeklyGroup">
                                <label>Weekly Days</label>
                                <div style="display:flex; gap:10px; flex-wrap:wrap;">
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Mon" />Mon</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Tue" />Tue</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Wed" />Wed</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Thu" />Thu</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Fri" />Fri</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Sat" />Sat</label>
                                  <label class="check"><input type="checkbox" class="editWeeklyDay" value="Sun" />Sun</label>
                                </div>
                              </div>
                              <div class="full" id="editMonthlyGroup">
                                <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
                                  <div>
                                    <label for="editMonthlyMode">Monthly Mode</label>
                                    <select id="editMonthlyMode">
                                      <option value="day_of_month">Day of Month</option>
                                      <option value="nth_weekday">Nth Weekday</option>
                                    </select>
                                  </div>
                                  <div id="editMonthlyDayWrap">
                                    <label for="editMonthlyDay">Day of Month (1-31)</label>
                                    <input id="editMonthlyDay" type="number" min="1" max="31" value="1" />
                                  </div>
                                  <div id="editMonthlyNthWrap">
                                    <label for="editMonthlyNth">Week Position</label>
                                    <select id="editMonthlyNth">
                                      <option value="1">First</option>
                                      <option value="2">Second</option>
                                      <option value="3">Third</option>
                                      <option value="4">Fourth</option>
                                      <option value="-1">Last</option>
                                    </select>
                                  </div>
                                  <div id="editMonthlyWeekdayWrap">
                                    <label for="editMonthlyWeekday">Weekday</label>
                                    <select id="editMonthlyWeekday">
                                      <option value="Mon">Monday</option>
                                      <option value="Tue">Tuesday</option>
                                      <option value="Wed">Wednesday</option>
                                      <option value="Thu">Thursday</option>
                                      <option value="Fri">Friday</option>
                                      <option value="Sat">Saturday</option>
                                      <option value="Sun">Sunday</option>
                                    </select>
                                  </div>
                                </div>
                              </div>
                              <div class="full" id="editOnceGroup">
                                <label for="editOnceDate">One-Off Date</label>
                                <input id="editOnceDate" type="date" />
                              </div>
                              <div class="full" id="editIntervalGroup">
                                <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
                                  <div>
                                    <label for="editIntervalEvery">Every (e.g. 10s, 5m, 2h)</label>
                                    <input id="editIntervalEvery" type="text" value="30s" />
                                  </div>
                                  <div>
                                    <label for="editTimerType">Timer Type</label>
                                    <select id="editTimerType">
                                      <option value="scheduled">Scheduled</option>
                                      <option value="daemon">Daemon</option>
                                    </select>
                                  </div>
                                  <div>
                                    <label for="editOverlapPolicy">Overlap Policy</label>
                                    <select id="editOverlapPolicy">
                                      <option value="skip">Skip</option>
                                      <option value="queue">Queue</option>
                                      <option value="allow">Allow</option>
                                    </select>
                                  </div>
                                </div>
                              </div>
                              <div>
                                <label for="editCommandMode">Command Mode</label>
                                <select id="editCommandMode">
                                  <option value="shell">Shell</option>
                                  <option value="exec">Exec</option>
                                </select>
                              </div>
                              <div class="full" id="editShellWrap">
                                <label for="editShell">Shell Command</label>
                                <input id="editShell" type="text" />
                              </div>
                              <div id="editExecWrap">
                                <label for="editExecutable">Executable</label>
                                <input id="editExecutable" type="text" />
                              </div>
                              <div id="editArgsWrap">
                                <label for="editArgs">Args (space separated)</label>
                                <input id="editArgs" type="text" />
                              </div>
                              <div>
                                <label for="editWorkingDirectory">Working Directory</label>
                                <input id="editWorkingDirectory" type="text" />
                              </div>
                              <div>
                                <label for="editWakeLead">Wake Lead (minutes)</label>
                                <input id="editWakeLead" type="number" min="0" max="240" />
                              </div>
                              <label class="check"><input id="editEnabled" type="checkbox" />Timer Enabled</label>
                              <label class="check"><input id="editWakeEnabled" type="checkbox" />Program Wake</label>
                              <label class="check"><input id="editNotifySuccess" type="checkbox" />Notify On Success</label>
                              <label class="check"><input id="editNotifyFailure" type="checkbox" />Notify On Failure</label>
                              <label class="check"><input id="editSlackActivity" type="checkbox" />Post Slack Activity</label>
                            </form>
                          </div>

                          <h3>Run Logs</h3>
                          <div id="modalRunLive" class="run-live hidden">
                            <span class="run-live-dot"></span>
                            <span id="modalRunLiveText">Run is still executing. Refreshing logs...</span>
                          </div>
                          <pre class="status" id="modalRunMeta">No run selected.</pre>
                          <div class="small-note">`stdout` = normal output, `stderr` = error/debug output (often empty on successful runs).</div>
                          <div class="grid-two">
                            <div>
                              <div class="small-note">stdout</div>
                              <pre class="mono" id="modalStdout"></pre>
                            </div>
                            <div>
                              <div class="small-note">stderr</div>
                              <pre class="mono" id="modalStderr"></pre>
                            </div>
                          </div>
                        </div>
                      </div>

                      <script>
                        if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(function(){}); }
                        const TERMINAL_STATUSES = new Set(['success', 'failed', 'aborted', 'uncertain_crash', 'skipped']);
                        let timerById = {};
                        let runById = {};
                        let activeTimerId = null;
                        let activeRunId = null;
                        let runPollTimeout = null;

                        function el(id) {
                          return document.getElementById(id);
                        }

                        function buildIdempotencyKey() {
                          return `ui-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
                        }

                        function displayStatus(run) {
                          if (run.status === 'success') return 'Completed Successfully';
                          if (run.status === 'failed') return 'Failed';
                          if (run.status === 'uncertain_crash') return 'Failed (Recovered After Crash)';
                          if (run.status === 'aborted') return 'Aborted';
                          if (run.status === 'waiting') return 'Waiting';
                          if (run.status === 'started') return 'Running...';
                          return run.status || '';
                        }

                        function displayMessage(run) {
                          if (run.status === 'success') return 'Completed Successfully';
                          if (run.status === 'aborted') return 'Aborted by user';
                          if (run.status === 'waiting') return 'Not ready yet \u2014 will retry';
                          return run.message || '';
                        }

                        function stopRunPolling() {
                          if (runPollTimeout) {
                            clearTimeout(runPollTimeout);
                            runPollTimeout = null;
                          }
                        }

                        function setRunLiveIndicator(isRunning, text = 'Run is still executing. Refreshing logs...') {
                          el('modalRunLive').classList.toggle('hidden', !isRunning);
                          el('modalRunLiveText').textContent = text;
                        }

                        function refreshAbortButton() {
                          const editMode = !el('modalEditWrap').classList.contains('hidden');
                          const activeRun = activeRunId ? runById[activeRunId] : null;
                          const showAbort = !editMode && Boolean(activeRun && activeRun.status === 'started');
                          el('modalAbort').classList.toggle('hidden', !showAbort);
                        }

                        function openModal() {
                          el('timerModal').classList.remove('hidden');
                        }

                        function closeModal() {
                          stopRunPolling();
                          activeTimerId = null;
                          activeRunId = null;
                          el('timerModal').classList.add('hidden');
                          el('modalStatus').textContent = '';
                          setRunLiveIndicator(false);
                          refreshAbortButton();
                          el('modalRunMeta').textContent = 'No run selected.';
                          el('modalStdout').textContent = '';
                          el('modalStderr').textContent = '';
                        }

                        function setDialogEditMode(editMode) {
                          el('modalReadonlyWrap').classList.toggle('hidden', editMode);
                          el('modalEditWrap').classList.toggle('hidden', !editMode);
                          el('modalEdit').classList.toggle('hidden', editMode);
                          el('modalRunNow').classList.toggle('hidden', editMode || !activeTimerId);
                          el('modalDelete').classList.toggle('hidden', editMode || !activeTimerId);
                          el('modalSave').classList.toggle('hidden', !editMode);
                          el('modalCancel').classList.toggle('hidden', !editMode);
                          refreshAbortButton();
                        }

                        function updateRecurrenceVisibility() {
                          const frequency = document.getElementById('frequency').value;
                          const monthlyMode = document.getElementById('monthlyMode').value;
                          const isInterval = frequency === 'interval';

                          document.getElementById('weeklyGroup').classList.toggle('hidden', frequency !== 'weekly');
                          document.getElementById('monthlyGroup').classList.toggle('hidden', frequency !== 'monthly');
                          document.getElementById('onceGroup').classList.toggle('hidden', frequency !== 'once');
                          document.getElementById('intervalGroup').classList.toggle('hidden', !isInterval);
                          document.getElementById('time').parentElement.classList.toggle('hidden', isInterval);
                          document.getElementById('lead').parentElement.classList.toggle('hidden', isInterval);

                          document.getElementById('monthlyDayWrap').classList.toggle('hidden', monthlyMode !== 'day_of_month');
                          const nthMode = monthlyMode === 'nth_weekday';
                          document.getElementById('monthlyNthWrap').classList.toggle('hidden', !nthMode);
                          document.getElementById('monthlyWeekdayWrap').classList.toggle('hidden', !nthMode);
                        }

                        function updateEditRecurrenceVisibility() {
                          const frequency = el('editFrequency').value;
                          const monthlyMode = el('editMonthlyMode').value;
                          const isInterval = frequency === 'interval';
                          el('editWeeklyGroup').classList.toggle('hidden', frequency !== 'weekly');
                          el('editMonthlyGroup').classList.toggle('hidden', frequency !== 'monthly');
                          el('editOnceGroup').classList.toggle('hidden', frequency !== 'once');
                          el('editIntervalGroup').classList.toggle('hidden', !isInterval);
                          el('editTime').parentElement.classList.toggle('hidden', isInterval);
                          el('editMonthlyDayWrap').classList.toggle('hidden', monthlyMode !== 'day_of_month');
                          const nthMode = monthlyMode === 'nth_weekday';
                          el('editMonthlyNthWrap').classList.toggle('hidden', !nthMode);
                          el('editMonthlyWeekdayWrap').classList.toggle('hidden', !nthMode);
                        }

                        function updateEditCommandVisibility() {
                          const mode = el('editCommandMode').value;
                          const shell = mode === 'shell';
                          el('editShellWrap').classList.toggle('hidden', !shell);
                          el('editExecWrap').classList.toggle('hidden', shell);
                          el('editArgsWrap').classList.toggle('hidden', shell);
                        }

                        function recurrenceFromCreateForm() {
                          const frequency = el('frequency').value;
                          if (frequency === 'interval') {
                            const every = (el('intervalEvery').value || '').trim();
                            if (!every) throw new Error('interval "every" value is required (e.g. 10s, 5m, 2h)');
                            return { frequency: 'interval', every };
                          }
                          const recurrence = { frequency, time: el('time').value, interval: 1 };
                          if (frequency === 'weekly') {
                            const weeklyDays = Array.from(document.querySelectorAll('.weeklyDay:checked')).map((n) => n.value);
                            if (weeklyDays.length === 0) throw new Error('select at least one weekday');
                            recurrence.weekly_days = weeklyDays;
                          }
                          if (frequency === 'monthly') {
                            const monthlyMode = el('monthlyMode').value;
                            recurrence.monthly_mode = monthlyMode;
                            if (monthlyMode === 'day_of_month') {
                              const day = Number(el('monthlyDay').value || '0');
                              if (!day || day < 1 || day > 31) throw new Error('day of month must be 1-31');
                              recurrence.day_of_month = day;
                            } else {
                              recurrence.nth = Number(el('monthlyNth').value || '1');
                              recurrence.weekday = el('monthlyWeekday').value;
                            }
                          }
                          if (frequency === 'once') {
                            const date = el('onceDate').value;
                            if (!date) throw new Error('one-off date is required');
                            recurrence.date = date;
                          }
                          return recurrence;
                        }

                        function recurrenceFromEditForm() {
                          const frequency = el('editFrequency').value;
                          if (frequency === 'interval') {
                            const every = (el('editIntervalEvery').value || '').trim();
                            if (!every) throw new Error('interval "every" value is required (e.g. 10s, 5m, 2h)');
                            return { frequency: 'interval', every };
                          }
                          const recurrence = { frequency, time: el('editTime').value, interval: 1 };
                          if (frequency === 'weekly') {
                            const weeklyDays = Array.from(document.querySelectorAll('.editWeeklyDay:checked')).map((n) => n.value);
                            if (weeklyDays.length === 0) throw new Error('select at least one weekday');
                            recurrence.weekly_days = weeklyDays;
                          }
                          if (frequency === 'monthly') {
                            const monthlyMode = el('editMonthlyMode').value;
                            recurrence.monthly_mode = monthlyMode;
                            if (monthlyMode === 'day_of_month') {
                              const day = Number(el('editMonthlyDay').value || '0');
                              if (!day || day < 1 || day > 31) throw new Error('day of month must be 1-31');
                              recurrence.day_of_month = day;
                            } else {
                              recurrence.nth = Number(el('editMonthlyNth').value || '1');
                              recurrence.weekday = el('editMonthlyWeekday').value;
                            }
                          }
                          if (frequency === 'once') {
                            const date = el('editOnceDate').value;
                            if (!date) throw new Error('one-off date is required');
                            recurrence.date = date;
                          }
                          return recurrence;
                        }

                        async function createTimer(event) {
                          event.preventDefault();
                          const status = document.getElementById('createStatus');
                          status.textContent = 'creating...';

                          const name = document.getElementById('name').value.trim();
                          const lead = Number(document.getElementById('lead').value || '0');
                          const shell = document.getElementById('shell').value.trim() || ':';
                          const cwd = document.getElementById('cwd').value.trim();
                          const enabled = document.getElementById('enabled').checked;
                          const wakeEnabled = document.getElementById('wakeEnabled').checked;
                          const notifySuccess = document.getElementById('notifySuccess').checked;
                          const notifyFailure = document.getElementById('notifyFailure').checked;
                          const slackActivity = document.getElementById('slackActivity').checked;

                          if (!name) {
                            status.textContent = 'error: name is required';
                            return;
                          }
                          if (!document.getElementById('time').value) {
                            status.textContent = 'error: run time is required';
                            return;
                          }
                          if (Number.isNaN(lead) || lead < 0) {
                            status.textContent = 'error: wake lead must be >= 0';
                            return;
                          }

                          let recurrence;
                          try {
                            recurrence = recurrenceFromCreateForm();
                          } catch (e) {
                            status.textContent = `error: ${e.message}`;
                            return;
                          }

                          const isInterval = recurrence.frequency === 'interval';
                          const payload = {
                            idempotency_key: buildIdempotencyKey(),
                            name,
                            comment: name,
                            enabled,
                            timezone: 'local',
                            recurrence,
                            command: {
                              mode: 'shell',
                              shell,
                              workingDirectory: cwd || '/Users/example'
                            },
                            wake: isInterval ? { enabled: false, action: 'wake', leadMinutes: 0 } : {
                              enabled: wakeEnabled,
                              action: 'wake',
                              leadMinutes: lead
                            },
                            notifications: {
                              onSuccess: notifySuccess,
                              onFailure: notifyFailure,
                              slackActivity
                            }
                          };
                          if (isInterval) {
                            payload.timer_type = el('timerType').value;
                            payload.execution = { overlap: el('overlapPolicy').value };
                          }

                          const resp = await fetch('/v1/timers', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                          });
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            status.textContent = `error: ${data.error || 'failed to create timer'}`;
                            return;
                          }

                          const timer = data.timer || {};
                          status.textContent = `created timer ${timer.id || ''} (${timer.name || name})`;
                          load();
                        }

                        function populateEditForm(timer) {
                          el('editName').value = timer.name || '';
                          el('editEnabled').checked = Boolean(timer.enabled);
                          el('editTimezone').value = timer.timezone || 'local';

                          const recurrence = timer.recurrence || {};
                          el('editFrequency').value = recurrence.frequency || 'daily';
                          el('editTime').value = recurrence.time || '00:00';
                          el('editMonthlyMode').value = recurrence.monthly_mode || 'day_of_month';
                          el('editMonthlyDay').value = recurrence.day_of_month || 1;
                          el('editMonthlyNth').value = recurrence.nth ?? 1;
                          el('editMonthlyWeekday').value = recurrence.weekday || 'Mon';
                          el('editIntervalEvery').value = recurrence.every || '30s';
                          el('editTimerType').value = timer.timer_type || 'scheduled';
                          el('editOverlapPolicy').value = (timer.execution || {}).overlap || 'skip';
                          el('editOnceDate').value = recurrence.date || new Date().toISOString().slice(0, 10);
                          Array.from(document.querySelectorAll('.editWeeklyDay')).forEach((n) => {
                            n.checked = Array.isArray(recurrence.weekly_days) && recurrence.weekly_days.includes(n.value);
                          });

                          const command = timer.command || {};
                          el('editCommandMode').value = command.mode || 'shell';
                          el('editShell').value = command.shell || '';
                          el('editExecutable').value = command.executable || '';
                          el('editArgs').value = Array.isArray(command.args) ? command.args.join(' ') : '';
                          el('editWorkingDirectory').value = command.workingDirectory || '/Users/example';

                          const wake = timer.wake || {};
                          el('editWakeEnabled').checked = Boolean(wake.enabled);
                          el('editWakeLead').value = Number(wake.leadMinutes || 0);

                          const notifications = timer.notifications || {};
                          el('editNotifySuccess').checked = Boolean(notifications.onSuccess);
                          el('editNotifyFailure').checked = notifications.onFailure !== false;
                          el('editSlackActivity').checked = notifications.slackActivity !== false;

                          updateEditRecurrenceVisibility();
                          updateEditCommandVisibility();
                        }

                        function editPayloadFromForm() {
                          const mode = el('editCommandMode').value;
                          const args = (el('editArgs').value || '').trim();
                          const recurrence = recurrenceFromEditForm();
                          const isInterval = recurrence.frequency === 'interval';
                          const result = {
                            name: el('editName').value.trim(),
                            enabled: el('editEnabled').checked,
                            timezone: (el('editTimezone').value || 'local').trim(),
                            recurrence,
                            command: {
                              mode,
                              shell: mode === 'shell' ? (el('editShell').value || ':').trim() : undefined,
                              executable: mode === 'exec' ? (el('editExecutable').value || '').trim() : undefined,
                              args: mode === 'exec' ? (args ? args.split(/\\s+/) : []) : undefined,
                              workingDirectory: (el('editWorkingDirectory').value || '/Users/example').trim(),
                            },
                            wake: {
                              enabled: el('editWakeEnabled').checked,
                              action: 'wake',
                              leadMinutes: Number(el('editWakeLead').value || '0'),
                            },
                            notifications: {
                              onSuccess: el('editNotifySuccess').checked,
                              onFailure: el('editNotifyFailure').checked,
                              slackActivity: el('editSlackActivity').checked,
                            },
                          };
                          if (isInterval) {
                            result.timer_type = el('editTimerType').value;
                            result.execution = { overlap: el('editOverlapPolicy').value };
                          }
                          return result;
                        }

                        async function showRunLogs(runId) {
                          const resp = await fetch(`/v1/runs/${runId}/logs`);
                          const payload = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalRunMeta').textContent = `Failed loading logs for ${runId}: ${payload.error || 'unknown error'}`;
                            setRunLiveIndicator(false);
                            activeRunId = null;
                            refreshAbortButton();
                            el('modalStdout').textContent = '';
                            el('modalStderr').textContent = '';
                            return;
                          }
                          const run = payload.run || {};
                          activeRunId = run.run_id || runId;
                          if (run.run_id) runById[run.run_id] = run;
                          const availability = [];
                          const isRunning = run.status === 'started';
                          if (isRunning && !payload.stdout_available && !payload.stderr_available) {
                            availability.push('initializing log stream');
                          } else {
                            if (!payload.stdout_available) availability.push('stdout log unavailable');
                            if (!payload.stderr_available) availability.push('stderr log unavailable');
                          }
                          if (payload.logs_expired) availability.push('logs expired (retention 7 days)');
                          const suffix = availability.length ? ` | ${availability.join(', ')}` : '';
                          el('modalRunMeta').textContent =
                            `Run ${run.run_id || runId} | ${displayStatus(run)} | Scheduled ${run.scheduled_at || ''}${suffix}`;
                          setRunLiveIndicator(isRunning);
                          refreshAbortButton();
                          const stdout = payload.stdout || '';
                          const stderr = payload.stderr || '';
                          el('modalStdout').textContent = stdout;
                          el('modalStderr').textContent = stderr;
                          // Show useful context when both logs are empty and run is terminal
                          if (!stdout.trim() && !stderr.trim() && run.status && run.status !== 'started') {
                            let ctx = '(no output produced)\\n\\n--- Run Context ---\\n';
                            ctx += 'Status: ' + displayStatus(run) + '\\n';
                            ctx += 'Exit code: ' + (run.exit_code != null ? run.exit_code : 'N/A') + '\\n';
                            ctx += 'Scheduled: ' + (run.scheduled_at || 'N/A') + '\\n';
                            ctx += 'Started: ' + (run.started_at || 'N/A') + '\\n';
                            ctx += 'Finished: ' + (run.finished_at || 'N/A') + '\\n';
                            if (run.started_at && run.finished_at) {
                              const dur = (new Date(run.finished_at) - new Date(run.started_at)) / 1000;
                              ctx += 'Duration: ' + dur.toFixed(1) + 's\\n';
                            }
                            if (run.timer_snapshot) {
                              try {
                                const snap = JSON.parse(run.timer_snapshot);
                                const cmd = snap.command || {};
                                ctx += '\\n--- Command ---\\n';
                                if (cmd.mode === 'shell') ctx += 'Shell: ' + (cmd.shell || 'N/A') + '\\n';
                                else ctx += 'Executable: ' + (cmd.executable || 'N/A') + ' ' + (cmd.args || []).join(' ') + '\\n';
                                if (cmd.workingDirectory) ctx += 'Working dir: ' + cmd.workingDirectory + '\\n';
                              } catch(e) {}
                            }
                            if (run.message) ctx += '\\nMessage: ' + run.message + '\\n';
                            el('modalStdout').textContent = ctx;
                          }
                        }

                        async function pollRunUntilTerminal(timerId, scheduledAt) {
                          stopRunPolling();
                          const tick = async () => {
                            if (!activeTimerId || activeTimerId !== timerId) return;
                            const runsResp = await fetch(`/v1/runs?limit=30&timer_id=${encodeURIComponent(timerId)}`);
                            const runsPayload = await runsResp.json().catch(() => ({ runs: [] }));
                            const runs = runsPayload.runs || [];
                            const run = runs.find((r) => r.scheduled_at === scheduledAt);
                            if (!run) {
                              runPollTimeout = setTimeout(tick, 1500);
                              return;
                            }
                            runById[run.run_id] = run;
                            await showRunLogs(run.run_id);
                            if (TERMINAL_STATUSES.has(run.status)) {
                              stopRunPolling();
                              return;
                            }
                            runPollTimeout = setTimeout(tick, 1500);
                          };
                          await tick();
                        }

                        async function pollRunByIdUntilTerminal(runId) {
                          stopRunPolling();
                          const tick = async () => {
                            if (el('timerModal').classList.contains('hidden')) return;
                            await showRunLogs(runId);
                            const current = runById[runId];
                            if (current && TERMINAL_STATUSES.has(current.status)) {
                              stopRunPolling();
                              return;
                            }
                            runPollTimeout = setTimeout(tick, 1200);
                          };
                          await tick();
                        }

                        async function openTimerDialog(timerId, preferredRunId) {
                          stopRunPolling();
                          activeTimerId = timerId;
                          activeRunId = null;
                          el('modalStatus').textContent = '';
                          const resp = await fetch(`/v1/timers/${timerId}`);
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalTitle').textContent = `Timer ${timerId}`;
                            el('modalReadonly').textContent = `Failed loading timer: ${data.error || 'unknown error'}`;
                            setDialogEditMode(false);
                            openModal();
                            return;
                          }
                          const timer = data.timer || {};
                          timerById[timer.id] = timer;
                          el('modalTitle').textContent = timer.name || timer.id || 'Timer';
                          el('modalReadonly').textContent = JSON.stringify(timer, null, 2);
                          populateEditForm(timer);
                          setDialogEditMode(false);
                          openModal();

                          if (preferredRunId) {
                            await showRunLogs(preferredRunId);
                            const preferred = runById[preferredRunId];
                            if (preferred && !TERMINAL_STATUSES.has(preferred.status)) {
                              await pollRunByIdUntilTerminal(preferredRunId);
                            }
                            return;
                          }

                          const runsResp = await fetch(`/v1/runs?limit=10&timer_id=${encodeURIComponent(timerId)}`);
                          const runsPayload = await runsResp.json().catch(() => ({ runs: [] }));
                          const latest = (runsPayload.runs || [])[0];
                          if (latest) {
                            runById[latest.run_id] = latest;
                            await showRunLogs(latest.run_id);
                            if (!TERMINAL_STATUSES.has(latest.status)) {
                              await pollRunByIdUntilTerminal(latest.run_id);
                            }
                          } else {
                            setRunLiveIndicator(false);
                            activeRunId = null;
                            refreshAbortButton();
                            el('modalRunMeta').textContent = 'No run history for this timer yet.';
                            el('modalStdout').textContent = '';
                            el('modalStderr').textContent = '';
                          }
                        }

                        async function openRunLogsFromRow(run) {
                          if (timerById[run.timer_id]) {
                            await openTimerDialog(run.timer_id, run.run_id);
                            return;
                          }
                          activeTimerId = null;
                          setDialogEditMode(false);
                          el('modalRunNow').classList.add('hidden');
                          el('modalDelete').classList.add('hidden');
                          el('modalEdit').classList.add('hidden');
                          el('modalTitle').textContent = run.timer_name || 'Deleted timer';
                          if (run.timer_snapshot) {
                            try {
                              el('modalReadonly').textContent = JSON.stringify(JSON.parse(run.timer_snapshot), null, 2);
                            } catch(e) {
                              el('modalReadonly').textContent = run.timer_snapshot;
                            }
                          } else {
                            el('modalReadonly').textContent = 'Timer definition is unavailable (timer deleted, no snapshot). Run logs are still viewable.';
                          }
                          openModal();
                          await showRunLogs(run.run_id);
                          if (!TERMINAL_STATUSES.has(run.status)) {
                            await pollRunByIdUntilTerminal(run.run_id);
                          }
                        }

                        async function saveTimerEdits() {
                          if (!activeTimerId) return;
                          let patch;
                          try {
                            patch = editPayloadFromForm();
                          } catch (e) {
                            el('modalStatus').textContent = `error: ${e.message}`;
                            return;
                          }
                          const resp = await fetch(`/v1/timers/${activeTimerId}`, {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ idempotency_key: buildIdempotencyKey(), ...patch }),
                          });
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalStatus').textContent = `save failed: ${data.error || 'unknown error'}`;
                            return;
                          }
                          const timer = data.timer || {};
                          timerById[timer.id] = timer;
                          el('modalReadonly').textContent = JSON.stringify(timer, null, 2);
                          el('modalTitle').textContent = timer.name || timer.id || 'Timer';
                          setDialogEditMode(false);
                          el('modalStatus').textContent = 'saved';
                          await load();
                        }

                        async function runNowFromDialog() {
                          if (!activeTimerId) return;
                          const resp = await fetch(`/v1/timers/${activeTimerId}/run-now`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ idempotency_key: buildIdempotencyKey() }),
                          });
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalStatus').textContent = `run-now failed: ${data.error || 'unknown error'}`;
                            return;
                          }
                          if (data.queued === false) {
                            el('modalStatus').textContent = `run-now was not enqueued at ${data.scheduled_at || ''} (already queued or duplicate second)`;
                            await load();
                            return;
                          }
                          el('modalStatus').textContent = `queued run-now at ${data.scheduled_at || ''}`;
                          await load();
                          await pollRunUntilTerminal(activeTimerId, data.scheduled_at);
                        }

                        async function abortRunFromDialog() {
                          if (!activeRunId) {
                            el('modalStatus').textContent = 'abort failed: no active run selected';
                            return;
                          }
                          const runId = activeRunId;
                          const resp = await fetch(`/v1/runs/${runId}/abort`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ idempotency_key: buildIdempotencyKey() }),
                          });
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalStatus').textContent = `abort failed: ${data.error || 'unknown error'}`;
                            return;
                          }
                          if (data.aborted) {
                            el('modalStatus').textContent = data.message || 'abort requested';
                          } else {
                            el('modalStatus').textContent = data.message || 'run is not active';
                          }
                          await showRunLogs(runId);
                          const latest = runById[runId];
                          if (latest && !TERMINAL_STATUSES.has(latest.status)) {
                            await pollRunByIdUntilTerminal(runId);
                          }
                          await load();
                        }

                        async function deleteTimerFromDialog() {
                          if (!activeTimerId) return;
                          const timerName = (timerById[activeTimerId] || {}).name || activeTimerId;
                          if (!confirm(`Delete timer "${timerName}"? This cannot be undone.`)) return;
                          const resp = await fetch(`/v1/timers/${activeTimerId}`, {
                            method: 'DELETE',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ idempotency_key: buildIdempotencyKey() }),
                          });
                          const data = await resp.json().catch(() => ({}));
                          if (!resp.ok) {
                            el('modalStatus').textContent = `delete failed: ${data.error || 'unknown error'}`;
                            return;
                          }
                          closeModal();
                          await load();
                        }

                        function describeRepeat(t) {
                          const r = t.recurrence || {};
                          const f = r.frequency;
                          if (t.timer_type === 'daemon') return 'continuous';
                          if (f === 'interval') return 'every ' + (r.every || '?');
                          const time = r.time || '';
                          if (f === 'daily') return 'daily at ' + time;
                          if (f === 'weekly') return (r.weekly_days || []).join(',') + ' at ' + time;
                          if (f === 'monthly') return 'monthly at ' + time;
                          if (f === 'once') return 'once ' + (r.date || '') + ' ' + time;
                          return f || '?';
                        }

                        function describeResources(t) {
                          const res = t.resources || [];
                          if (!res.length) return '-';
                          return res.map(r => r.name || '?').join(', ');
                        }

                        async function toggleEnabled(timerId, currentlyEnabled, event) {
                          event.stopPropagation();
                          const action = currentlyEnabled ? 'disable' : 'enable';
                          await fetch('/v1/timers/' + timerId + '/' + action, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ idempotency_key: buildIdempotencyKey() }),
                          });
                          await load();
                        }

                        async function load() {
                          const health = await fetch('/v1/health').then(r => r.json());
                          document.getElementById('health').textContent = JSON.stringify(health, null, 2);

                          const muted = Boolean(health.notifications_muted);
                          const muteBtn = el('muteToggle');
                          muteBtn.textContent = muted ? 'MUTED' : 'ON';
                          muteBtn.className = muted ? 'toggle-btn toggle-off' : 'toggle-btn toggle-on';

                          const timers = await fetch('/v1/timers').then(r => r.json());
                          timerById = {};
                          const tBody = document.querySelector('#timers tbody');
                          tBody.innerHTML = '';
                          for (const t of timers.timers || []) {
                            timerById[t.id] = t;
                            const tr = document.createElement('tr');
                            tr.className = 'timer-row-clickable';
                            const tType = t.timer_type === 'daemon' ? 'daemon' : (t.recurrence && t.recurrence.frequency === 'interval' ? 'interval' : 'scheduled');

                            const tdName = document.createElement('td'); tdName.textContent = t.name; tr.appendChild(tdName);
                            const tdType = document.createElement('td'); tdType.textContent = tType; tr.appendChild(tdType);
                            const tdRepeat = document.createElement('td'); tdRepeat.textContent = describeRepeat(t); tr.appendChild(tdRepeat);

                            const tdEnabled = document.createElement('td');
                            const toggleBtn = document.createElement('button');
                            toggleBtn.textContent = t.enabled ? 'ON' : 'OFF';
                            toggleBtn.className = t.enabled ? 'toggle-btn toggle-on' : 'toggle-btn toggle-off';
                            toggleBtn.addEventListener('click', (e) => toggleEnabled(t.id, t.enabled, e));
                            tdEnabled.appendChild(toggleBtn);
                            tr.appendChild(tdEnabled);

                            const tdRes = document.createElement('td'); tdRes.textContent = describeResources(t); tr.appendChild(tdRes);
                            const tdNext = document.createElement('td'); tdNext.textContent = t.next_run || ''; tr.appendChild(tdNext);
                            const tdDel = document.createElement('td');
                            tdDel.style.textAlign = 'center';
                            const delBtn = document.createElement('button');
                            delBtn.className = 'row-delete-btn';
                            delBtn.textContent = '\\u00d7';
                            delBtn.title = 'Delete timer';
                            delBtn.addEventListener('click', async (e) => {
                              e.stopPropagation();
                              if (!confirm('Delete timer "' + t.name + '"? This cannot be undone.')) return;
                              const resp = await fetch('/v1/timers/' + t.id, {
                                method: 'DELETE',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ idempotency_key: buildIdempotencyKey() }),
                              });
                              if (resp.ok) await load();
                            });
                            tdDel.appendChild(delBtn);
                            tr.appendChild(tdDel);
                            tr.addEventListener('click', () => openTimerDialog(t.id));
                            tBody.appendChild(tr);
                          }

                          const runs = await fetch('/v1/runs?limit=20').then(r => r.json());
                          runById = {};
                          const rBody = document.querySelector('#runs tbody');
                          rBody.innerHTML = '';
                          for (const run of runs.runs || []) {
                            runById[run.run_id] = run;
                            const tr = document.createElement('tr');
                            if (run.status === 'success') tr.className = 'run-success';
                            if (run.status === 'failed' || run.status === 'uncertain_crash') tr.className = 'run-failed';
                            if (run.status === 'started') tr.className = 'run-running';
                            if (run.status === 'aborted') tr.className = 'run-aborted';
                            if (run.status === 'waiting') tr.className = 'run-waiting';
                            const timerLabel = run.timer_name || timerById[run.timer_id]?.name || 'Deleted timer';
                            tr.innerHTML = `<td>${timerLabel}</td><td>${displayStatus(run)}</td><td>${run.scheduled_at || ''}</td><td>${displayMessage(run)}</td><td><button class="logs-link secondary" data-run-id="${run.run_id}">View Logs</button></td>`;
                            rBody.appendChild(tr);
                          }
                          Array.from(document.querySelectorAll('.logs-link')).forEach((btn) => {
                            btn.addEventListener('click', (event) => {
                              event.stopPropagation();
                              const runId = btn.getAttribute('data-run-id');
                              const run = runById[runId];
                              if (run) {
                                openRunLogsFromRow(run);
                              }
                            });
                          });
                        }
                        const today = new Date().toISOString().slice(0, 10);
                        document.getElementById('onceDate').value = today;
                        document.getElementById('frequency').addEventListener('change', updateRecurrenceVisibility);
                        document.getElementById('monthlyMode').addEventListener('change', updateRecurrenceVisibility);
                        document.getElementById('createForm').addEventListener('submit', createTimer);
                        document.getElementById('modalClose').addEventListener('click', closeModal);
                        document.getElementById('modalEdit').addEventListener('click', () => setDialogEditMode(true));
                        document.getElementById('modalCancel').addEventListener('click', () => {
                          if (activeTimerId && timerById[activeTimerId]) populateEditForm(timerById[activeTimerId]);
                          setDialogEditMode(false);
                          el('modalStatus').textContent = '';
                        });
                        document.getElementById('modalSave').addEventListener('click', saveTimerEdits);
                        document.getElementById('modalRunNow').addEventListener('click', runNowFromDialog);
                        document.getElementById('modalAbort').addEventListener('click', abortRunFromDialog);
                        document.getElementById('modalDelete').addEventListener('click', deleteTimerFromDialog);
                        document.getElementById('editFrequency').addEventListener('change', updateEditRecurrenceVisibility);
                        document.getElementById('editMonthlyMode').addEventListener('change', updateEditRecurrenceVisibility);
                        document.getElementById('editCommandMode').addEventListener('change', updateEditCommandVisibility);
                        document.getElementById('timerModal').addEventListener('click', (event) => {
                          if (event.target === document.getElementById('timerModal')) closeModal();
                        });
                        document.addEventListener('keydown', (event) => {
                          if (event.key !== 'Escape') return;
                          const modal = document.getElementById('timerModal');
                          if (modal.classList.contains('hidden')) return;
                          const editMode = !el('modalEditWrap').classList.contains('hidden');
                          if (editMode) {
                            if (activeTimerId && timerById[activeTimerId]) populateEditForm(timerById[activeTimerId]);
                            setDialogEditMode(false);
                            el('modalStatus').textContent = '';
                          } else {
                            closeModal();
                          }
                        });
                        document.getElementById('muteToggle').addEventListener('click', async () => {
                          const current = el('muteToggle').textContent === 'ON';
                          await fetch('/v1/settings/notifications', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ muted: current }),
                          });
                          await load();
                        });
                        updateRecurrenceVisibility();
                        refreshAbortButton();
                        load();
                        setInterval(load, 10000);
                      </script>
                    </body>
                    </html>
                    """.strip()
                )
                return


            if method == "GET" and path == "/v1/health":
                self._send_json(200, self.service.health())
                return

            if method == "GET" and path == "/v1/settings/notifications":
                self._send_json(200, {"notifications_muted": self.service.get_notifications_muted()})
                return

            if method == "POST" and path == "/v1/settings/notifications":
                body = self._read_json()
                muted = body.get("muted")
                if muted is None:
                    raise ApiError(400, "muted field is required (boolean)")
                self._send_json(200, self.service.set_notifications_muted(bool(muted)))
                return

            if method == "GET" and path == "/v1/timers":
                self._send_json(200, {"timers": self.service.list_timers()})
                return

            if method == "GET" and path.startswith("/v1/timers/") and "/" not in path[len("/v1/timers/"):]:
                timer_id = path.split("/")[-1]
                timer = self.service.get_timer(timer_id)
                if not timer:
                    raise ApiError(404, f"timer '{timer_id}' not found")
                self._send_json(200, {"timer": timer})
                return

            if method == "POST" and path == "/v1/timers":
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                payload = {k: v for k, v in body.items() if k != "idempotency_key"}
                cb = payload.get("callback")
                if cb:
                    auto_capture_terminal(cb)
                self._send_json(200, self.service.create_timer(payload, idem_key))
                return

            if method == "PATCH" and path.startswith("/v1/timers/") and "/" not in path[len("/v1/timers/"):]:
                timer_id = path.split("/")[-1]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                patch = {k: v for k, v in body.items() if k != "idempotency_key"}
                self._send_json(200, self.service.update_timer(timer_id, patch, idem_key))
                return

            if method == "DELETE" and path.startswith("/v1/timers/") and "/" not in path[len("/v1/timers/"):]:
                timer_id = path.split("/")[-1]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.delete_timer(timer_id, idem_key))
                return

            if method == "POST" and path.endswith("/enable") and path.startswith("/v1/timers/"):
                timer_id = path.split("/")[-2]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.set_timer_enabled(timer_id, True, idem_key))
                return

            if method == "POST" and path.endswith("/disable") and path.startswith("/v1/timers/"):
                timer_id = path.split("/")[-2]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.set_timer_enabled(timer_id, False, idem_key))
                return

            if method == "POST" and path.endswith("/run-now") and path.startswith("/v1/timers/"):
                timer_id = path.split("/")[-2]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.run_timer_now(timer_id, idem_key))
                return

            if method == "POST" and path.endswith("/clone") and path.startswith("/v1/timers/"):
                timer_id = path.split("/")[-2]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                overrides = {k: v for k, v in body.items() if k != "idempotency_key"}
                self._send_json(200, self.service.clone_timer(timer_id, overrides, idem_key))
                return

            if method == "GET" and path == "/v1/templates":
                self._send_json(200, {"templates": self.service.list_templates()})
                return

            if method == "GET" and path.startswith("/v1/templates/"):
                tpl_name = path[len("/v1/templates/"):]
                try:
                    tpl = self.service.get_template(tpl_name)
                except KeyError:
                    raise ApiError(404, f"template '{tpl_name}' not found")
                self._send_json(200, {"template": tpl})
                return

            if method == "POST" and path == "/v1/timers/from-template":
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                template_name = body.get("template")
                if not template_name:
                    raise ApiError(400, "'template' field is required")
                overrides = body.get("overrides", {})
                cb = overrides.get("callback")
                if cb:
                    auto_capture_terminal(cb)
                try:
                    self._send_json(200, self.service.create_from_template(template_name, overrides, idem_key))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                return

            if method == "GET" and path == "/v1/system/wezterm-panes":
                import subprocess as _sp
                try:
                    result = _sp.run(
                        ["wezterm", "cli", "list", "--format", "json"],
                        capture_output=True, text=True, timeout=5,
                    )
                    panes = json.loads(result.stdout) if result.returncode == 0 else []
                except Exception:
                    panes = []
                self._send_json(200, {"panes": panes})
                return

            if method == "GET" and path == "/v1/runs":
                limit = int((q.get("limit") or ["100"])[0])
                timer_id = (q.get("timer_id") or [None])[0]
                self._send_json(200, {"runs": self.service.list_runs(limit=limit, timer_id=timer_id)})
                return

            if method == "GET" and path.startswith("/v1/runs/") and path.endswith("/logs"):
                parts = path.strip("/").split("/")
                run_id = parts[2]
                self._send_json(200, self.service.get_run_logs(run_id))
                return

            if method == "POST" and path.startswith("/v1/runs/") and path.endswith("/abort"):
                parts = path.strip("/").split("/")
                run_id = parts[2]
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.abort_run(run_id, idem_key))
                return

            if method == "GET" and path == "/v1/incidents":
                limit = int((q.get("limit") or ["200"])[0])
                include_acked = (q.get("include_acked") or ["true"])[0].lower() != "false"
                incidents = self.service.state.list_incidents(limit=limit, include_acked=include_acked)
                self._send_json(200, {"incidents": incidents})
                return

            if method == "POST" and path.startswith("/v1/incidents/") and path.endswith("/ack"):
                incident_id = int(path.split("/")[-2])
                body = self._read_json()
                idem_key = self._require_idempotency(body)
                self._send_json(200, self.service.ack_incident(incident_id, idem_key))
                return

            if method == "GET" and path == "/logo.png":
                logo_path = Path(__file__).with_name("logo.png")
                if logo_path.exists():
                    body = logo_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=604800")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_json(404, {"error": "logo not found"})
                return

            if method == "GET" and path == "/favicon.ico":
                svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                    '<circle cx="32" cy="32" r="30" fill="#1E1B4B"/>'
                    '<path d="M22 42 C22 28 25 22 32 19 C39 22 42 28 42 42Z" fill="#6366F1"/>'
                    '<rect x="19" y="42" width="26" height="3" rx="1.5" fill="#6366F1"/>'
                    '<circle cx="32" cy="48.5" r="2.5" fill="#6366F1"/>'
                    '<polygon points="35,22 28,34 33,33 29,44 39,30 34,31 37,22" fill="#FBBF24"/>'
                    '<line x1="32" y1="8" x2="32" y2="14" stroke="#FBBF24" stroke-width="2" stroke-linecap="round"/>'
                    '<line x1="23" y1="11" x2="25" y2="15" stroke="#FBBF24" stroke-width="1.5" stroke-linecap="round"/>'
                    '<line x1="41" y1="11" x2="39" y2="15" stroke="#FBBF24" stroke-width="1.5" stroke-linecap="round"/>'
                    '</svg>'
                )
                body = svg.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(body)
                return

            if method == "GET" and path == "/manifest.json":
                manifest = json.dumps({
                    "name": "WakeLite",
                    "short_name": "WakeLite",
                    "description": "Local wake & task scheduler",
                    "start_url": "/ui",
                    "display": "standalone",
                    "background_color": "#000000",
                    "theme_color": "#000000",
                    "icons": [
                        {"src": "/favicon.ico", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    ],
                })
                body = manifest.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if method == "GET" and path == "/sw.js":
                sw = "self.addEventListener('fetch', function(e) {});\n"
                body = sw.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Service-Worker-Allowed", "/")
                self.end_headers()
                self.wfile.write(body)
                return

            if method == "GET" and path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
                fname = path.lstrip("/")
                icon_path = Path(__file__).with_name(fname)
                if icon_path.exists():
                    body = icon_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=604800")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(302)
                    self.send_header("Location", "/favicon.ico")
                    self.end_headers()
                return

            raise ApiError(404, f"unknown route: {method} {path}")

        except ApiError as e:
            self._send_json(e.code, {"error": e.message})
        except KeyError as e:
            self._send_json(404, {"error": f"not found: {e.args[0]}"})
        except CapacityExceededError as e:
            self._send_json(409, {"error": str(e), "code": "CAPACITY_EXCEEDED"})
        except IdempotencyConflictError as e:
            self._send_json(409, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


class ApiServer:
    def __init__(self, service: WakeLiteService, host: str = API_HOST, port: int = API_PORT, socket_path: Path = SOCKET_PATH) -> None:
        self.service = service
        self.host = host
        self.port = port
        self.socket_path = socket_path

        self._tcp_server: Optional[ThreadingHTTPServer] = None
        self._uds_server: Optional[ThreadingUnixHTTPServer] = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        ApiHandler.service = self.service

        self._tcp_server = ThreadingHTTPServer((self.host, self.port), ApiHandler)
        t1 = threading.Thread(target=self._tcp_server.serve_forever, daemon=True, name="wakelite-api-tcp")
        t1.start()
        self._threads.append(t1)

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._uds_server = ThreadingUnixHTTPServer(str(self.socket_path), ApiHandler)
        t2 = threading.Thread(target=self._uds_server.serve_forever, daemon=True, name="wakelite-api-uds")
        t2.start()
        self._threads.append(t2)

    def stop(self) -> None:
        if self._tcp_server:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
        if self._uds_server:
            self._uds_server.shutdown()
            self._uds_server.server_close()
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
