# Operations

```bash
WL={{WAKELITE_REPO}}/bin/wakelitectl
```

## Install the services

Clone the repo somewhere permanent first. The generated plist hardcodes the checkout's absolute
path, so an install from `/tmp` breaks when macOS cleans that directory.

```bash
$WL launchd install --scope user --load      # runner (required)
$WL launchd status
$WL health
sudo $WL launchd install --scope system --load   # wake reconciler (optional, for wake-from-sleep)
```

Remove everything with `$WL launchd uninstall --scope user`, and `sudo $WL launchd uninstall --scope system --unload` for the reconciler.

After changing WakeLite code, run `$WL launchd restart`. It purges `__pycache__` and kickstarts
the runner.

## First command for any anomaly: `doctor`

`$WL doctor` reads `{{WAKELITE_HOME}}/state.db` directly, so it works even when the runner is hung.
It reports the runner, daemons, ports, and failing timers.

**Silent-stop trap:** a full disk stops every timer while the runner process still looks healthy.
Check free space and recent rows in the `run_history` table before debugging a single timer.

**Muted notifications trap:** if `$WL health` shows `notifications_muted: true` outside your quiet
hours, every `onFailure` alert fleet-wide is suppressed. Check this flag first when asking "why
wasn't I notified?".

## Look up a timer ID by name

```bash
ID=$($WL timer list | python3 -c "
import json, sys
for t in json.load(sys.stdin).get('timers', []):
    if t.get('name') == 'my-timer':
        print(t['id']); break
")
$WL timer delete "$ID" --idempotency-key "del-$(date +%s)"
```

## Idempotency gotchas

- **Duplicate timers:** a different `--idempotency-key` creates a new timer even with the same
  name. Delete the old timer first, or reuse the same key.
- **Self-deleting timers:** a script that deletes its own timer must look up the ID at runtime,
  because IDs change on recreate. Use the lookup above.

## Daemon timer gotchas

- **Clean exit is not restarted.** `restart_on_failure: true` treats exit 0 as an intentional
  stop. A daemon killed by SIGTERM during a runner restart can stay stopped.
- **Backoff accumulates.** Each runner restart sends SIGTERM to running daemons, and repeated
  exits build exponential backoff. Run `timer disable` then `timer enable` to clear it.
- **Disable does not kill.** `timer disable` stops future restarts, but the running process keeps
  going. Find and kill it, for example with `lsof -nP -iTCP:<port> -sTCP:LISTEN`.
- **Inspect daemon state directly:**
  ```bash
  sqlite3 {{WAKELITE_HOME}}/state.db "SELECT status, last_exit_code, last_started_at, current_backoff_seconds FROM daemon_state WHERE timer_id = '<uuid>'"
  ```
- **Client timeouts do not cancel server jobs.** A script that times out on a BigQuery (or other
  remote) job kills only the local process, and the remote job keeps running and billing. Give
  scheduled remote jobs a server-side cap.

## Wake from sleep

The reconciler reads `{{WAKELITE_HOME}}/wake-intents.json`, which the runner writes, and applies it
with `pmset schedule` every 10 minutes. It resolves the owning user's home from the owner of the
script file, so it does not read `/var/root`.

```bash
pmset -g sched                      # should list entries owned by com.wakelite
$WL reconcile --once --dry-run      # preview without installing
```

**Do not judge the reconciler by whether the Mac sleeps.** An active Claude Code session runs
`caffeinate -i -t 300`, a 5-minute rolling keepalive, so the Mac may never sleep during a session.
Check `pmset -g sched` instead.

## After a Mac restart

```bash
launchctl list | grep com.wakelite     # runner should show a PID
$WL health                             # status: ok
$WL timer list                         # expected timers enabled
```

Unacknowledged incidents are expected after a restart, because daemons received SIGTERM. Review
them with `$WL incidents`.
