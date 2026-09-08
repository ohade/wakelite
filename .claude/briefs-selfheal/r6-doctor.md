# R6 — `wakelitectl doctor`

Read `_common.md` in this directory first; it holds the incident, the test conventions,
the acceptance criteria, and the scope boundaries.

## Why this exists

When something goes wrong with WakeLite there is no single command that says what. Today
diagnosing the incident meant hand-querying SQLite, reading `lsof`, and grepping a 33MB
log. `doctor` is meant to be the first command a human or an agent runs, and later the
body of an automated watchdog.

## The one design constraint that shapes everything

The CLI talks only to the REST API — `_api_call` at `cli.py:20` posts to
`http://127.0.0.1:17341`. That is useless in the case that matters most, a hung runner.
So `doctor` must try REST first and fall back to opening the state database directly with
`StateStore` (`state.py:38`, constructor takes a `db_path`). It runs as the owning user,
so this creates no permission problem.

## The report (read-only)

- **Runner:** heartbeat age. The heartbeat is written at `service.py:762` at least every
  15 seconds (`_MAX_SLEEP` at `service.py:89`), so anything older than about 120 seconds
  means the loop is not turning. Say plainly whether the runner is healthy, stale, or
  unreachable.
- **Per daemon:** is it alive, its PID, whether its parent is the current runner process,
  and whether more than one process claims the same timer. Once R1 lands, children carry
  `WAKELITE_TIMER_ID` in their environment (visible via `ps -E -o args= -p <pid>`), which
  is how you detect a duplicate. Write this so it degrades gracefully if the marker is
  absent, since R1 may land after you.
- **Ports:** for any daemon declaring a port, who holds it
  (`lsof -nP -tiTCP:<port> -sTCP:LISTEN`) and whether that holder is ours.
- **Failing timers:** any timer with a failure streak of 3 or more, with the last line of
  its stderr log. R2 adds the streak columns to `timer_runtime`; if they are not present
  yet, derive the streak from recent `run_history` rows so this works standalone.
- **Incidents:** unacknowledged count by type. There are 142 right now.
- **Watchdog:** when it last ran, from a `meta` key such as `watchdog.last_run`.

## `--fix`

Exactly two actions, no more, and each one records an incident naming what it did:

1. The R1 orphan reclaim.
2. `launchctl kickstart -k gui/<uid>/com.wakelite.runner` when the heartbeat is
   stale, bounded to at most one kick every 30 minutes, with the last attempt persisted
   in `meta`. If two kicks within two hours fail to restore the heartbeat, stop kicking
   and raise one critical incident instead of looping.

Write the kick as a function that is unit-testable with the subprocess call injected or
patched. Running `launchctl` for real is out of scope for this session, per `_common.md`
— the lead verifies that live.

## `--quiet`

Print nothing when everything is healthy and nothing was fixed. This is what makes the
command safe to run on a schedule.

## Tests

Place them above the `__main__` guard.

1. A stale heartbeat is reported as stale; a fresh one as healthy.
2. With the REST endpoint unreachable, the report still works from the database
   directly. This is the important one — it is the hung-runner case.
3. `--fix` with a stale heartbeat calls the kick exactly once; a second call within 30
   minutes does not call it again.
4. After two failed kicks in two hours, a third check raises the critical incident and
   does not kick.
5. `--quiet` on a healthy system produces no output.
