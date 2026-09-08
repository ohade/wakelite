# R1 — Orphan reclaim by identity, all timer types

Read `_common.md` in this directory first; it holds the incident, the test conventions,
the acceptance criteria, and the scope boundaries.

This is the one item that prevents the incident recurring across a hard runner crash
(SIGKILL, power loss, launchd killing the runner at its exit timeout).

## Problem

When the runner dies without a clean shutdown, its children keep running
(`start_new_session=True`, `service.py:2266`). On the next start, `start()`
(`service.py:113`) calls `recover_uncertain_runs()` (`state.py:601`), marks the runs
`uncertain_crash`, and respawns them. Nothing checks whether the previous process is
still alive, so the old one keeps holding its port and every respawn fails.

## Two traps that make the obvious implementation wrong

1. **The PIDs are deleted before you can read them.** `start()` calls
   `set_runtime_idle(row["timer_id"])` at `service.py:118`. With no `run_id` argument
   that runs `DELETE FROM active_runs WHERE timer_id = ?` (`state.py:324`). So any
   reclaim must capture the `active_runs` rows BEFORE that line executes.
   `recover_uncertain_runs` itself only returns `run_id, timer_id, scheduled_at`
   (`state.py:601`) — it does not return the PID.

2. **Matching on the command line does not work.** The timer command is
   `exec /opt/homebrew/bin/python3 <script>`, but the live process reports argv[0] as
   `/opt/homebrew/Cellar/python@3.14/.../Python`. Verified on the live daemon. Identity
   must come from an environment marker instead.

## What to build

**Environment marker.** Where the child environment is built (`service.py:2234-2235`,
`env = os.environ.copy()` then `env.update(...)`), inject `WAKELITE_RUN_ID=<run_id>` and
`WAKELITE_TIMER_ID=<timer_id>`. This survives `exec` and argv rewriting. Verified on this
machine that `ps -E -o args= -p <pid>` exposes a child's environment.

**Startup reclaim**, in `start()` before `service.py:118`:

1. Read the `active_runs` rows (`run_id, timer_id, pid, started_at`).
2. Skip any row whose `started_at` predates `sysctl -n kern.boottime` — after a reboot
   that PID is certainly a different process.
3. `os.kill(pid, 0)` to test existence. Treat `PermissionError` as alive (this mirrors
   the existing branch at `service.py:2515`).
4. Confirm identity: `ps -E -o args= -p <pid>` output contains `WAKELITE_RUN_ID=<run_id>`.
5. On match: SIGTERM, wait up to 5s, SIGKILL if needed, and record an incident
   `orphan_reclaimed` naming the PID and the timer.
6. On mismatch: leave the process alone and record an incident `pid_reused`.
7. Then let the existing recovery continue unchanged.

Applies to ALL timer types, not just daemons. Two daemons (`slack-agent`,
`claude-callout`) declare no port, so an orphan there runs alongside a new instance and
nothing else would ever notice.

**Reuse rather than reinvent:** `state.get_active_run_pid` (`state.py:370`),
`state.update_active_run_pid` (`state.py:365`), `state.add_incident` (`state.py:735`),
and the process-liveness logic already in `_is_daemon_process_alive` (`service.py:2515`).

**Second, lower priority — pre-spawn port reclaim for daemons.** Add an optional
`port` key to `resources[]`. This requires adding it to `ALLOWED_RESOURCE_KEYS`
(`timer_store.py:23`), which is validated strictly at `timer_store.py:127`. Before
spawning a daemon that declares a port, attempt a `socket.bind`. On `EADDRINUSE`, find
the holder with `lsof -nP -tiTCP:<port> -sTCP:LISTEN` (measured at 40ms) and apply the
same env-marker identity check: reclaim if it is ours, otherwise do not spawn and record
one `resource_held_by_foreign` incident. Do the startup reclaim first and treat this as
a follow-on within the same task.

## Tests

Place them above the `__main__` guard. At minimum:

1. A child spawned through the real env-injection path, its row seeded into
   `active_runs`, is terminated by startup reclaim and produces an `orphan_reclaimed`
   incident. Do not hand-write a `sleep` process for this — a test that spawns its own
   `sleep` would pass even with command-line matching, which is exactly the broken
   approach, so it would prove nothing.
2. A live PID whose environment lacks the marker is left running and produces a
   `pid_reused` incident.
3. A row whose `started_at` predates boot time is skipped without any signal.

## Note on the two-part change

If the port work forces a schema key, remember a rolled-back runner rejects timers
carrying an unknown `resources[]` key (`timer_store.py:127-129`). Mention this in your
commit message so the lead can weigh the rollback cost.
