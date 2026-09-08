# R3 — A slow network must never delay or strand a run

Read `_common.md` in this directory first; it holds the incident, the test conventions,
the acceptance criteria, and the scope boundaries.

## Problem

The trigger for the whole incident was a network call sitting between the run row and
`Popen`. `_run_occurrence` posts "timer started" to Slack at `service.py:2215` before it
starts the process. After a boot there is no DNS yet, and `urlopen(timeout=10)`
(`notifier.py:188`) does not bound `getaddrinfo`, so the call stalled about 35 seconds —
longer than two nominal timeouts — and pushed `Popen` past the 30-second ghost window.

Commit `9754fbd` stopped the ghost check from misfiring. It did not stop the delay
itself. A daemon that should be up at boot is still 35 seconds late.

## Part 1 — move the Slack post after Popen

The thread id (`slack_thread_ts`) is only consumed after `proc.wait()` returns, so the
"started" post can happen after `Popen` without losing anything. Move it. The command
then starts on time even with no DNS at all.

## Part 2 — a readiness probe that never resolves a name

Add `_network_ready()`. It must NOT call `getaddrinfo`, since that is the exact call
that blocks. Both of these return in about 0ms on this machine:

- `route -n get default` succeeds when a default route exists
- `scutil --nwi` reports at least one reachable interface

Cache the result for the duration of a tick.

## Part 3 — the boot window wait

If the runner started less than 5 minutes ago and `_network_ready()` is false, the worker
waits inside `_run_occurrence` before `Popen`, polling every 5 seconds, capped at 120
seconds. Then it proceeds regardless.

**Do not implement this by recording the run as `waiting` (exit 75).** That looks
tempting and is wrong: `finish_run` with status `waiting` leaves the occurrence
`pending` (`state.py:576-582`), but `_process_due` only scans occurrences between the
last tick and now, so a calendar timer that reports "not ready" at boot is simply skipped
until its next scheduled occurrence — possibly the next day. For a daemon, a `waiting`
exit also doubles the restart backoff at `service.py:2615`.

The in-worker wait covers calendar timers, interval timers, daemons, and `run-now`
uniformly, and needs no schema change.

**One behaviour to get right:** skip the wait for a `run-now` invoked by a human outside
the boot window. A person triggering a run manually should not sit through a 120-second
delay.

For testability, allow an environment override such as `WAKELITE_FORCE_NETWORK_DOWN=1`
that makes the probe report "not ready". The lead needs this to verify the behaviour on
the live machine without blocking DNS for the whole machine.

## Tests

Place them above the `__main__` guard.

1. With the notifier stalled for 5 seconds, `Popen` still happens within about half a
   second. This is the direct regression for the incident. `DaemonSpawnStallTests` at
   `tests/test_service.py:3529` already shows how to stall the notifier in a real worker
   thread — reuse that technique.
2. With the probe patched to report "not ready" and then "ready", the run waits and then
   spawns within one poll interval.
3. A `run-now` outside the boot window does not wait even when the probe says not ready.
4. No occurrence is left `pending` and skipped by the network path — assert the run
   actually executed.
