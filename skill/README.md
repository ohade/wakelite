# wakelite-scheduling skill

A Claude Code skill that teaches Claude how to create, debug, and operate WakeLite timers.

## Install

```bash
./skill/install.sh --dry-run    # preview what it would write
./skill/install.sh              # install for this checkout
```

Then start a new Claude Code session. Ask something like "remind me at 15:00 to check the
deploy" and Claude will use the skill.

The skill describes the checkout it was installed from. If you move the clone, run the installer
again.

## What the installer does

The files in `template/` contain three placeholders. The installer replaces them and writes the
result to `~/.claude/skills/wakelite-scheduling/`.

| Placeholder | Source |
|-------------|--------|
| `{{WAKELITE_REPO}}` | `--repo`, else the checkout that contains this `skill/` folder |
| `{{WAKELITE_HOME}}` | `--home`, then `$WAKELITE_HOME`, then `~/.wakelite` |
| `{{INSTALL_DATE}}` | today's date |

It refuses to install when a placeholder is left unreplaced, or when a path contains spaces or
shell characters, because the skill has copy-paste shell lines. An existing skill is moved to
`<dest>.bak-<timestamp>`, never deleted. Run `./skill/install.sh --help` for every option.

## Contents

| File | Covers |
|------|--------|
| `SKILL.md` | Architecture, core CLI commands, quick-start reminder, templates, timer types |
| `references/timer-schemas.md` | Timer JSON formats, intervals, `active_hours`, `until`, validation errors |
| `references/callbacks.md` | Sending a timer result back into a Claude Code session, exit codes, `claude -p` in scripts |
| `references/operations.md` | Install, `doctor`, daemon gotchas, wake from sleep, post-restart checks |

When WakeLite's behaviour changes, update `template/` in the same commit.

Uninstall: `rm -rf ~/.claude/skills/wakelite-scheduling`.
