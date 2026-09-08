# Shared context for every WakeLite self-healing brief

## The repo and where you work

Python. You are already in a dedicated git worktree cut from commit `9754fbd` on
branch `codex/public-readiness-20260628`. Work only in this worktree. Commit on the
branch this worktree is on.

Run tests with the repo convention — `PYTHONPATH=.` is required or imports fail:

    PYTHONPATH=. /opt/homebrew/bin/python3 -m pytest tests/test_service.py -q

## The incident this work exists to prevent

On 2026-09-07 the daemon timer `cmux-focus-server` failed 37 restarts in a row with
`OSError: [Errno 48] Address already in use` on TCP 17382.

`_run_occurrence` (`wakelite/service.py:2178`) creates the run row, then posts a
"timer started" message to Slack at `service.py:2215` before reaching `Popen`. Right
after a machine boot that post stalled about 35 seconds on DNS. `_process_daemons`
(`service.py:2549`) saw a running runtime row whose run had no PID for longer than
`DAEMON_SPAWN_GRACE_SECONDS` (`service.py:2513`, 30s), declared it a ghost, cleared
it, and respawned. The original `Popen` still fired seconds later, so the real server
ran outside `_active_processes` and held the port against every later restart.

Commit `9754fbd` fixed the in-process race with an in-memory `_spawning_runs` set.
It does NOT fix the crash case: if the runner is SIGKILLed or the machine loses power,
children survive (`start_new_session=True`, `service.py:2266`) and nothing reclaims them.

## Test conventions you must follow

- `_bootstrap(temp_home)` at `tests/test_service.py:16` builds a service against a
  temporary HOME and stubs the notifier. Use it.
- `DaemonSpawnStallTests` at `tests/test_service.py:3529` is the template for a test
  that drives a real worker thread and asserts on run status. `DaemonTimerTests` at
  `tests/test_service.py:417` is the template for daemon lifecycle tests.
- Place any new test class ABOVE the `if __name__ == "__main__":` guard at the end of
  the file. A class below it is invisible to a direct
  `PYTHONPATH=. python tests/test_service.py <ClassName>` run.

## Acceptance criteria — all four required

1. A test that exercises the reported behaviour exists.
2. You ran it against the UNFIXED code and it FAILED. Record the command, and the
   assertion or error text.
3. You then made the change and ran the SAME test, unchanged, and it PASSED.
4. `PYTHONPATH=. /opt/homebrew/bin/python3 -m pytest tests/test_service.py -q` is green
   (baseline is 123 passed, 39 subtests; your new tests add to that).

A test that passes before your change does not exercise the behaviour — rewrite it.
Never relax a check to reach green; if something blocks you, stop and report the block.

## Scope boundaries

This task is code and tests inside the worktree. The following belong to the lead and
are out of scope for this session — leave them for the lead to do:

- `launchctl` in any form, and `wakelitectl launchd *`
- anything under `~/.wakelite` (the live state directory)
- restarting or deploying the runner
- editing `~/git/playground/amq-coop-setup/PRODUCTION-CMUX-ACCEPTANCE.md` or running
  any acceptance matrix
- files outside this worktree

## What to return

The commit, plus three logs pasted in full: the red run, the green run, and the full
suite result.
