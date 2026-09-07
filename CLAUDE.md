# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Run all tests (pytest preferred over unittest discover)
PYTHONPATH=. python -m pytest tests/ -v

# Run a single test class or method
PYTHONPATH=. python -m pytest tests/test_service.py::UntilConditionTests -v
PYTHONPATH=. python -m pytest tests/test_service.py::UntilConditionTests::test_until_success_deletes_on_success -v

# Run recurrence tests only
PYTHONPATH=. python -m pytest tests/test_recurrence.py -v

# CLI commands (always need PYTHONPATH)
PYTHONPATH=. ./bin/wakelitectl health
PYTHONPATH=. ./bin/wakelitectl doctor # first command to run when something is wrong
PYTHONPATH=. ./bin/wakelitectl timer list
PYTHONPATH=. ./bin/wakelitectl launchd restart    # restart runner after code changes (purges __pycache__)
```

No dependencies to install — zero external packages for runtime (stdlib only). Optional: `rumps` for menubar app.

## Architecture

WakeLite is a local macOS scheduler with four independently running components:

```
┌─────────────────────────────────────────────────────────┐
│ runner (bin/wakelite-runner)                             │
│   WakeLiteService  ──  scheduler loop (threading)       │
│   ApiServer        ──  REST API on :17341 + Unix socket │
│   MCP HTTP         ──  JSON-RPC on :17342 (optional)    │
└─────────────────────────────────────────────────────────┘
  bin/wakelitectl ── CLI client (talks to REST API)
  bin/wakelite-mcp ── MCP stdio proxy (talks to REST API)
  bin/wakelite-reconciler ── system-level pmset wake scheduling (runs as root)
```

The **runner** is the single source of truth. Everything else is a client that talks to its REST API.

### Core modules (dependency order)

| Module | Role |
|--------|------|
| `config.py` | Paths, ports, constants. `WAKELITE_HOME` env overrides `~/.wakelite` |
| `utils.py` | `atomic_write_json`, ISO parsing |
| `recurrence.py` | Pure functions: parse recurrence specs, compute next occurrences. No I/O |
| `timer_store.py` | Timer CRUD on `~/.wakelite/timers.json`. Validates schema, thread-safe |
| `state.py` | SQLite (`~/.wakelite/state.db`) — run history, runtime state, daemon state, incidents, incident ignore rules |
| `notifier.py` | Desktop + Slack notifications. Desktop alerts are posted by `notifier-app/` (WakeLiteNotify.app) so a click opens the dashboard; falls back to terminal-notifier, then osascript |
| `notifier.py` | macOS notifications (`osascript`) + Slack DMs (`notify_slack()`) |
| `capacity.py` | Time-axis per-resource admission gate. Projects each timer's resource use onto N-minute buckets over a 7-day horizon, blocks create/update when any resource's peak > capacity. Pure functions, no I/O. `_executor.slot` is a well-known resource (capacity=MAX_WORKERS); user-declared `resources[]` participate when `capacity`/`estimated_usage` parse |
| `service.py` | **Core orchestrator.** Scheduler loop, run execution (ThreadPoolExecutor), timer lifecycle |
| `http_api.py` | REST API handler + embedded web UI (single-file HTML/CSS/JS in Python string) |
| `mcp_server.py` | MCP protocol bridge — translates MCP tool calls to REST API calls |
| `reconciler.py` | Reads timer wake intents, reconciles with `pmset schedule` entries |
| `doctor.py` | `wakelitectl doctor` — read-only diagnosis of heartbeat, daemons, ports, failure streaks, incidents. The only client that bypasses REST: it falls back to opening `state.db` directly, because a hung runner is exactly when the API stops answering. `--fix` does two bounded things (orphan reclaim, rate-limited `launchctl kickstart`) and records an incident for each |

### Data storage

- **Timers**: `~/.wakelite/timers.json` (JSON file, atomic writes)
- **State**: `~/.wakelite/state.db` (SQLite WAL mode — run_history, occurrences, daemon_state, incidents, meta)
- **Run logs**: `~/.wakelite/logs/{timer_id}/{date}/{run_id}.{out,err}.log` (7-day retention)
- **Runner log**: `~/.wakelite/logs/runner.log`

### Timer types

- **Scheduled** (`timer_type: "scheduled"`, default): one-shot commands on a recurrence (daily/weekly/monthly/once/interval)
- **Daemon** (`timer_type: "daemon"`): long-lived process kept alive with restart/backoff. Uses `"every": "0s"` recurrence

### Run lifecycle in service.py

```
_tick() → _schedule_occurrence() → _spawn_run() → _run_occurrence() [in thread pool]
  └→ subprocess.Popen (shell command)
  └→ finish_run() → until check → max_runs check → notifications → queue drain
```

Key invariant: `_run_occurrence()` runs in the ThreadPoolExecutor. It holds no scheduler lock during command execution.

### Idempotency

All mutating operations (create, update, delete, enable, disable, run-now, abort) require an `idempotency_key`. Keys are stored in `state.db` with 24-hour TTL. Duplicate keys return the original result.

## Testing patterns

Tests in `tests/test_service.py` use a `_bootstrap(temp_dir)` helper that redirects `WAKELITE_HOME` to a temp directory and reloads all modules. This gives each test a fresh timer store and state DB.

Timer execution tests call `svc._schedule_occurrence()` directly and `time.sleep()` to wait for the thread pool. Typical wait is 1.5s for simple commands.

The MCP test (`test_mcp_and_manifest.py`) is flaky — it tests that MCP fails fast when the runner is down, but passes when the runner is actually running.

## Web UI

The entire web UI is an HTML string embedded in `http_api.py` (served at `/ui`). There are no separate HTML/JS/CSS files. Edits to the UI mean editing the Python string in `http_api.py`.

## Deployment

The runner runs as a launchd user agent (`com.wakelite.runner`). After code changes:

1. `PYTHONPATH=. ./bin/wakelitectl launchd restart` — purges `__pycache__` and kickstarts the service
2. The plist sets `PYTHONDONTWRITEBYTECODE=1` to prevent stale bytecode

The reconciler runs as a system-level launchd daemon (requires `sudo` to install).

## Monitor script conventions

Monitor/polling scripts MUST produce stdout describing what they're doing. Silent scripts make debugging impossible — even when exit codes are correct, empty logs give zero visibility into what happened.

1. **Echo what you're checking**: `echo "Checking PR-98729 build #9 status..."`
2. **Echo the result**: `echo "Build status: $STATUS"`
3. **Echo exit reason before exit 75**: `echo "Not ready yet — status=$STATUS"`
4. **Exit codes**: `0` = done/success, `75` = not ready (waiting), `1` = error

Example:
```bash
#!/bin/bash
echo "Checking PR-98729 build #9..."
STATUS=$(jk get-build-status 98729 9)
echo "Build status: $STATUS"
if [ "$STATUS" = "SUCCESS" ]; then
    echo "Build completed successfully"
    exit 0
fi
echo "Build still running — will check again"
exit 75
```

Note: Even if a script produces no output, the web UI will show run context (command, exit code, timestamps) — but explicit logging is always preferred.

## Capacity gate (WL-12)

`service.check_capacity()` gates timer create/update via `capacity.py`, which
projects each timer's resource use onto a time axis and blocks when any
declared resource's peak concurrent use would exceed its capacity.

**Semantic change from the pre-WL-12 gate:** the old implementation summed
`_estimate_slots()` across every timer regardless of when they run, so a
daily 06:00 backup and a once-timer scheduled for 2026-05-04 09:00 competed
for the same slot budget even though they never coexist at runtime. The new
gate is strictly more permissive in that common case and strictly correct
when two timers actually overlap in time. HTTP contract is preserved:
capacity violations still return 409 with `code: CAPACITY_EXCEEDED`.

**Resources.** Every timer implicitly consumes `_executor.slot` (capacity
= `MAX_WORKERS` = 16). Daemons count as exactly 1 slot regardless of
`execution.overlap`/`max_concurrent` — the daemon runtime keeps a single
live process. Non-daemon timers with `overlap="allow"` consume
`max_concurrent` slots. User-declared `resources[]` (e.g. `slack-api`,
`ollama-gpu`) participate when their `capacity`/`estimated_usage` strings
parse (`"50 req/min"`, `"12 req/sec"`, `"1 req/h"`, bare integers). When
multiple timers declare the same resource with different capacities, the
MIN is used — deterministic and conservative.

**Interval phasing.** `service.check_capacity` enriches existing interval
timers with `_last_fired_at` from `state.db` meta before calling the
engine, so the projection uses the real scheduler phase rather than
collapsing every interval timer to `now`. Brand-new intervals without
history are projected from `now` (conservative).

**Env knobs.** All optional, positive integers (invalid values silently
fall back to defaults):

| Env var | Default | Effect |
|---|---|---|
| `WAKELITE_CAPACITY_ENABLED` | `true` | `false`/`0`/`no`/`off`/`disabled` bypasses the gate — admission-control rollback lever without code revert. |
| `WAKELITE_CAPACITY_HORIZON_DAYS` | `7` | How far forward the projection looks. |
| `WAKELITE_CAPACITY_BUCKET_SECONDS` | `60` | Bucket granularity. Smaller = tighter collision detection, larger = cheaper. |
| `WAKELITE_CAPACITY_RUN_DURATION_SECONDS` | `60` | Assumed duration of one run, used to decide which buckets a fire occupies. |

Bucket arithmetic uses `.timestamp()` on datetimes so DST transitions
don't skew the 7-day axis.

## Boot-window network wait

`_run_occurrence` starts the process first and posts "timer started" to Slack
afterwards, so a stalled network can no longer delay `Popen`. On top of that,
when the runner has been up for less than `BOOT_NETWORK_WINDOW_SECONDS` (300)
and `_network_ready()` is false, the worker polls every
`NETWORK_WAIT_POLL_SECONDS` (5) for at most `NETWORK_WAIT_MAX_SECONDS` (120)
before starting the command anyway.

`_network_ready()` never resolves a name — `getaddrinfo` is the call that blocks
for tens of seconds after a boot. It shells out to `route -n get default`, then
`scutil --nwi`, both of which answer in about a millisecond, and fails open if
neither answers. The result is cached for one scheduler tick.

Set `WAKELITE_FORCE_NETWORK_DOWN=1` to make the probe report "not ready" without
touching the machine's real networking — this is how the wait is verified live.
## Orphan reclaim (startup)

Children are spawned with `start_new_session=True`, so a SIGKILLed or power-cut
runner leaves them running. `start()` calls `_reclaim_orphaned_processes()`
**before** the recovery loop, because `set_runtime_idle(timer_id)` with no
`run_id` deletes the `active_runs` rows that hold the PIDs.

Each row is (1) skipped if `started_at` predates `sysctl -n kern.boottime`,
(2) skipped if the PID is gone, (3) identity-checked, then SIGTERM → 5s →
SIGKILL, recording an `orphan_reclaimed` incident. A live PID that fails the
identity check is left alone with a `pid_reused` incident; one that cannot be
judged gets `orphan_unverified` and is also left alone.

**Identity is process start time, not the command line and not the environment.**

- Command line does not work: the timer runs `exec /opt/homebrew/bin/python3
  <script>` but the live process reports argv[0] as
  `/opt/homebrew/Cellar/python@3.14/.../Python`.
- Environment does not work on macOS 26: `ps -E` (and `KERN_PROCARGS2` directly)
  return only argv for a non-root caller, even for your own child. Measured on
  26.5.1 build 25F80. `WAKELITE_RUN_ID` / `WAKELITE_TIMER_ID` are still injected
  into every child and still checked first — they carry the check if the runner
  ever runs as root, and on Linux where `/proc/<pid>/environ` is readable.
- So `active_runs.pid_started_at` records `ps -o lstart=` at spawn, and reclaim
  compares it. A recycled PID has a different start time. Rows written before
  this column existed have no start time, so their processes are left alone.

`resources[].port` is optional and opt-in. A daemon that declares one gets a
`socket.bind` probe before spawn; the holder is found with `lsof` and reclaimed
if it is ours (env marker, or an open fd on this timer's run log — the proof
that survives the `active_runs` row being deleted). A foreign holder blocks the
spawn with a `resource_held_by_foreign` incident instead of burning the restart
budget on EADDRINUSE.

## Key gotchas

- **Shell-mode timers run under `/bin/zsh -lc`; `status` is a read-only zsh special parameter.** Never write `status=$(...)` in a timer command; use a specific name such as `build_status`. Observed 2026-08-02: a long-lived polling timer repeatedly failed with `zsh: read-only variable: status` before reaching its polling logic.
- **Interval format is single-unit only.** `5h2m` is rejected — use `302m` instead.
- **`scripts/` is gitignored.** Timer scripts live there but aren't tracked.
- **Timer JSON validation is strict.** Unknown top-level keys are rejected. See `ALLOWED_PAYLOAD_KEYS` in `timer_store.py`.
- **`until` deletes timers; `max_runs` disables them.** First limit reached wins when both are set.
- **Slack activity is opt-out per timer.** `notifications.slackActivity` defaults to `true` (including when absent on legacy timers). Set it to `false` for local-only monitors; this suppresses run-start, run-end, and auto-delete Slack lifecycle writes. Callback fallbacks remain a separate delivery path.
- **Slack token** for `notifier.notify_slack()` is read from `~/.claude.json` → `mcpServers.slack.env.SLACK_BOT_TOKEN`.
- **Exit code 75 = "waiting/not ready yet".** Polling scripts should `exit 75` (not `exit 1`) when the condition isn't met yet. WakeLite shows these as blue "Waiting" rows instead of red "Failed". Exit 75 does not trigger failure notifications, incidents, or until conditions.
