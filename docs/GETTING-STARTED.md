# Getting Started

WakeLite has a lot of surface — a scheduler, a dashboard, a CLI, terminal
callbacks — and the README documents all of it. This page is the other thing
you want on day one: **a prompt that makes an AI agent onboard you against your
own machine**, instead of you reading reference docs and guessing which parts
apply.

If you use Claude Code, Codex, Cursor, or any agent that can read a repo and run
commands, clone WakeLite, open the agent **inside the clone**, and paste the
block below. It will read the real code, look at what you already run, and
propose timers that fit you rather than generic examples.

You do not need this to use WakeLite. [`README.md`](../README.md) is complete on
its own. This is a shortcut, not a dependency.

## The prompt

```
I just cloned WakeLite and I'm new to it. You're in the repo, so read the
actual code before answering — don't guess.

Start by reading README.md, then CLAUDE.md for the architecture, then
docs/API.md for the full timer schema, and .claude/rules/timer-authoring-gotchas.md
for the traps.

Then walk me through four things, in this order:

1. WHAT IT IS. In a short paragraph: what problem WakeLite solves that cron and
   launchd don't, and specifically what "wakes the Mac from sleep" buys me.
   Tell me what it does NOT do, so I don't misuse it.

2. GET ME RUNNING. Check my machine's actual state first — is the runner
   installed as a launchd agent, is it running, does `./bin/wakelitectl health`
   answer? Then give me the exact commands for whatever is missing. Tell me
   whether I need the system-scope reconciler (sudo) or whether user scope is
   enough for what I want, and explain the difference in one sentence.

3. HOW TO DRIVE IT DAILY. I want both paths:
   - The web dashboard at http://127.0.0.1:17341/ui — what each part of the
     screen is for, how to create a timer without writing JSON, where to see
     run history and logs, and what the row colours mean.
   - The CLI — walk me through the subcommands I'll actually use
     (timer list/create/update/run-now/delete, runs list/logs, doctor,
     incidents, launchd). For each, one real example I can paste. Explain
     --idempotency-key once, properly, because every mutating command wants it.

4. WHAT'S WORTH SETTING UP. Based on what you can see on my machine
   (my shell history, my repos, what launchd agents I already have, what I
   seem to do repeatedly), suggest 5-8 concrete timers that would genuinely
   help ME. For each: what it does, the recurrence, whether it needs
   wake-from-sleep, and the exact JSON. Don't suggest generic examples —
   look at my actual setup first and justify each one.

Rules while you do this:
- Don't create, modify, or delete any timer without asking me first.
- Don't touch ~/.wakelite/ — that's live state.
- If something needs configuration (Slack, callbacks), say what it needs and
  what happens if I skip it. I want to know what's optional.
- Flag anything in the repo that is still hardcoded to one machine, so I'm
  not surprised by it.

At the end, give me a single "first week" checklist: the 3 things to do now,
and the 3 things to come back to once I've seen it run.
```

## Two things to know before you run it

These are worth knowing up front, because an agent inspecting your machine
cannot warn you about them in advance.

**Clone somewhere permanent before installing.** The generated launchd plist
hardcodes the absolute path of this checkout. Installing from `/tmp` leaves
launchd pointing at a directory macOS will delete. If you already did that,
move the clone and rerun `launchd install`. To undo an install entirely:

```bash
./bin/wakelitectl launchd uninstall --scope user
```

**Do not `pip install -e .`.** `[project.scripts]` in `pyproject.toml` would put
`wakelitectl` and four sibling console scripts onto whatever interpreter you
used. WakeLite runs straight from the clone — the wrappers in `bin/` set
`PYTHONPATH` themselves and prefer `.venv/bin/python` when it exists.

## If you would rather not use an agent

Read these in order:

1. [`README.md`](../README.md) — requirements, install, the full CLI reference,
   terminal callbacks, and the configuration table
2. [`API.md`](API.md) — every timer field, all recurrence types, execution
   controls, and the REST API
3. [`../.claude/rules/timer-authoring-gotchas.md`](../.claude/rules/timer-authoring-gotchas.md)
   — the schema traps worth reading before you write your first timer by hand
