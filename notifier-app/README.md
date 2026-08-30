# WakeLiteNotify

Posts WakeLite's desktop notifications so that **clicking one opens the
dashboard page the alert is about**.

## Why a bundle is needed

`osascript`'s `display notification` cannot carry a click action, and macOS
attributes those notifications to Script Editor — so clicking a WakeLite alert
opened Script Editor's document picker. Homebrew's `terminal-notifier` can
carry a click target, but on macOS 26 its adhoc-signed bundle is refused
notification permission outright (`exit 3`, never listed in System Settings,
`tccutil reset` fails).

Posting from a bundle WakeLite owns gives the notification its own bundle
identity, its own permission grant, and a delegate that acts on the click.

## Build and grant permission

```bash
./build-app.sh
open ~/Applications/WakeLiteNotify.app   # required once — grants permission
```

The `open` step is not optional. Executing the binary straight from a shell is
refused with `Notifications are not allowed for this application`; launching it
through LaunchServices once is what registers it and lets the grant happen.

Verify:

```bash
~/Applications/WakeLiteNotify.app/Contents/MacOS/WakeLiteNotify \
  --title "WakeLite" --message "click me" \
  --url "http://127.0.0.1:17341/ui#incidents"; echo $?   # 0 = working
```

## Modes

- **post** — `--title T --message M [--url U] [--group G]`. `--group` doubles as
  the notification identifier, so a repeatedly failing timer replaces its own
  previous alert instead of stacking up. Non-zero exit means WakeLite should
  fall back, so a denial never silently drops an alert.
- **click** — no arguments. macOS relaunches the app when one of its
  notifications is clicked; the delegate opens the URL from `userInfo`.

`wakelite/notifier.py` picks the first working poster and drops any that fails,
so a permission denial costs one subprocess per process lifetime, not one per
notification.
