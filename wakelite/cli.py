from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request
from uuid import uuid4

from .config import API_HOST, API_PORT, auto_capture_terminal
from .launchd_install import install_system, install_user, status as launchd_status, uninstall_system, uninstall_user
from .mcp_manifest import generate_manifest, install_global_configs, manual_snippets
from .reconciler import main as reconciler_main
from .runner_main import main as runner_main


def _api_call(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"http://{API_HOST}:{API_PORT}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url=url, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": body or str(e)}
        raise RuntimeError(parsed.get("error", f"HTTP {e.code}"))
    except Exception:
        raise RuntimeError("WakeLite service unavailable. Start with: wakelitectl serve")


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_warnings(response: Dict[str, Any]) -> None:
    for w in response.get("warnings", []):
        print(f"\033[33mwarning:\033[0m {w}", file=sys.stderr)


def _cmd_manifest(args: argparse.Namespace) -> None:
    manifest = generate_manifest(args.mcp_command)
    _print_json(manifest)


def _cmd_mcp_install(args: argparse.Namespace) -> None:
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    result = install_global_configs(args.mcp_command, targets)
    manifest = generate_manifest(args.mcp_command)
    _print_json({"installed": result, "manifest": manifest})


def _cmd_mcp_print(args: argparse.Namespace) -> None:
    _print_json(manual_snippets(args.mcp_command))


def _cmd_health(args: argparse.Namespace) -> None:
    _print_json(_api_call("GET", "/v1/health"))


def _cmd_timer_list(args: argparse.Namespace) -> None:
    _print_json(_api_call("GET", "/v1/timers"))


def _cmd_timer_create(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    cb = payload.get("callback")
    if cb:
        auto_capture_terminal(cb)
    payload["idempotency_key"] = args.idempotency_key
    _print_json(_api_call("POST", "/v1/timers", payload))


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("--enabled must be true/false")


def _cmd_timer_update(args: argparse.Namespace) -> None:
    if args.file:
        patch = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        patch = {}
        if args.name is not None:
            patch["name"] = args.name
        if args.time is not None:
            patch["recurrence"] = {"time": args.time}
        if args.comment is not None:
            patch["comment"] = args.comment
        if args.shell is not None:
            patch["command"] = {"shell": args.shell}
        if args.enabled is not None:
            patch["enabled"] = args.enabled

        if not patch:
            raise RuntimeError("timer update requires --file or at least one inline patch flag")

    patch["idempotency_key"] = args.idempotency_key
    result = _api_call("PATCH", f"/v1/timers/{args.timer_id}", patch)
    _print_json(result)
    _print_warnings(result)


def _cmd_timer_enable_disable(args: argparse.Namespace, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    _print_json(
        _api_call(
            "POST",
            f"/v1/timers/{args.timer_id}/{action}",
            {"idempotency_key": args.idempotency_key},
        )
    )


def _cmd_timer_delete(args: argparse.Namespace) -> None:
    _print_json(
        _api_call(
            "DELETE",
            f"/v1/timers/{args.timer_id}",
            {"idempotency_key": args.idempotency_key},
        )
    )


def _cmd_timer_run_now(args: argparse.Namespace) -> None:
    _print_json(
        _api_call(
            "POST",
            f"/v1/timers/{args.timer_id}/run-now",
            {"idempotency_key": args.idempotency_key},
        )
    )


def _cmd_timer_clone(args: argparse.Namespace) -> None:
    overrides: Dict[str, Any] = {}
    if args.name:
        overrides["name"] = args.name
    if args.patch_file:
        overrides.update(json.loads(Path(args.patch_file).read_text(encoding="utf-8")))
    payload: Dict[str, Any] = {"idempotency_key": args.idempotency_key}
    payload.update(overrides)
    _print_json(_api_call("POST", f"/v1/timers/{args.timer_id}/clone", payload))


def _cmd_timer_from_template(args: argparse.Namespace) -> None:
    overrides: Dict[str, Any] = {"name": args.name}
    if args.comment:
        overrides["comment"] = args.comment
    if args.shell:
        overrides["command"] = {"mode": "shell", "shell": args.shell}
    rec: Dict[str, Any] = {}
    if args.time:
        rec["time"] = args.time
    if args.date:
        rec["date"] = args.date
    if args.every:
        rec["every"] = args.every
    if rec:
        overrides["recurrence"] = rec
    # Auto-capture terminal ID for callback templates
    if args.template == "callback":
        cb = overrides.setdefault("callback", {})
        auto_capture_terminal(cb)
    payload: Dict[str, Any] = {
        "template": args.template,
        "overrides": overrides,
        "idempotency_key": args.idempotency_key,
    }
    _print_json(_api_call("POST", "/v1/timers/from-template", payload))


def _cmd_template_list(args: argparse.Namespace) -> None:
    _print_json(_api_call("GET", "/v1/templates"))


def _cmd_template_show(args: argparse.Namespace) -> None:
    _print_json(_api_call("GET", f"/v1/templates/{args.name}"))


def _cmd_runs_list(args: argparse.Namespace) -> None:
    query = f"?limit={args.limit}"
    if args.timer_id:
        query += f"&timer_id={args.timer_id}"
    _print_json(_api_call("GET", f"/v1/runs{query}"))


def _cmd_runs_logs(args: argparse.Namespace) -> None:
    _print_json(_api_call("GET", f"/v1/runs/{args.run_id}/logs"))


def _cmd_runs_abort(args: argparse.Namespace) -> None:
    _print_json(
        _api_call(
            "POST",
            f"/v1/runs/{args.run_id}/abort",
            {"idempotency_key": args.idempotency_key},
        )
    )


def _cmd_incidents_list(args: argparse.Namespace) -> None:
    include = "true" if args.include_acked else "false"
    _print_json(_api_call("GET", f"/v1/incidents?limit={args.limit}&include_acked={include}"))




def _cmd_launchd_install(args: argparse.Namespace) -> None:
    if args.scope in ("system", "both") and os.geteuid() != 0:
        raise RuntimeError(
            "system launchd install requires sudo: "
            "sudo ./bin/wakelitectl launchd install --scope system --load"
        )
    payload = {}
    if args.scope in ("user", "both"):
        payload["user"] = install_user(load=args.load)
    if args.scope in ("system", "both"):
        payload["system"] = install_system(load=args.load)
    _print_json(payload)


def _cmd_launchd_uninstall(args: argparse.Namespace) -> None:
    if args.scope in ("system", "both") and os.geteuid() != 0:
        raise RuntimeError(
            "system launchd uninstall requires sudo: "
            "sudo ./bin/wakelitectl launchd uninstall --scope system --unload"
        )
    payload = {}
    if args.scope in ("user", "both"):
        payload["user"] = uninstall_user(unload=args.unload)
    if args.scope in ("system", "both"):
        payload["system"] = uninstall_system(unload=args.unload)
    _print_json(payload)


def _cmd_launchd_status(args: argparse.Namespace) -> None:
    _print_json(launchd_status())


def _cmd_launchd_restart(args: argparse.Namespace) -> None:
    import subprocess
    # Clean stale __pycache__ bytecode before restarting so the runner picks up code changes
    project_root = Path(__file__).resolve().parents[1]
    purged = 0
    for cache_dir in project_root.rglob("__pycache__"):
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)
        purged += 1

    uid = os.getuid()
    label = "com.wakelite.runner"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        _print_json({"status": "restarted", "label": label, "pycache_dirs_purged": purged})
    else:
        _print_json({"status": "error", "returncode": result.returncode, "stderr": result.stderr.strip()})


def _cmd_incidents_ack(args: argparse.Namespace) -> None:
    _print_json(
        _api_call(
            "POST",
            f"/v1/incidents/{args.incident_id}/ack",
            {"idempotency_key": args.idempotency_key},
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="WakeLite control CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run runner + API")
    serve.add_argument("--tick-seconds", type=int, default=15)
    serve.add_argument("--with-mcp-http", action="store_true")

    reconcile = sub.add_parser("reconcile", help="run wake reconciler")
    reconcile.add_argument("--once", action="store_true")
    reconcile.add_argument("--dry-run", action="store_true")
    reconcile.add_argument("--interval", type=int, default=600)

    manifest = sub.add_parser("manifest", help="generate manifest")
    manifest.add_argument("--mcp-command", default=str(Path(__file__).resolve().parents[1] / "bin" / "wakelite-mcp"))

    mcp = sub.add_parser("mcp", help="mcp config helpers")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_install = mcp_sub.add_parser("install", help="install global mcp config")
    mcp_install.add_argument("--targets", default="claude,codex")
    mcp_install.add_argument("--mcp-command", default=str(Path(__file__).resolve().parents[1] / "bin" / "wakelite-mcp"))

    mcp_print = mcp_sub.add_parser("print-config", help="print config snippets")
    mcp_print.add_argument("--mcp-command", default=str(Path(__file__).resolve().parents[1] / "bin" / "wakelite-mcp"))

    sub.add_parser("health", help="service health")

    timer = sub.add_parser("timer", help="timer operations")
    timer_sub = timer.add_subparsers(dest="timer_cmd", required=True)
    timer_sub.add_parser("list")

    timer_create = timer_sub.add_parser(
        "create",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Timer JSON schema:
{
  "name": "my-timer",                          # required
  "comment": "What this timer does",            # required
  "recurrence": {                               # required (NOT "schedule")
    "frequency": "daily|weekly|monthly|once|interval",
    "time": "HH:MM",                            #   for daily/weekly/monthly/once
    "date": "YYYY-MM-DD",                        #   required if frequency=once
    "every": "10s",                             #   required if frequency=interval (10s, 5m, 2h)
    "active_hours": {                           #   optional, interval only — daily window
      "start": "07:00",                         #     fire starting at this time each day
      "end": "22:30"                            #     stop firing after this time
    }
  },
  "timer_type": "scheduled|daemon",            # optional, default "scheduled"
  "execution": {                                # optional (v2)
    "overlap": "skip|queue|allow",              #   default "skip"
    "max_concurrent": 1,
    "restart_on_failure": false,                #   for daemon timers
    "restart_delay_seconds": 5,
    "restart_max_backoff_seconds": 300
  },
  "resources": [                                # optional (v2) - declare external resources
    { "name": "slack-api", "capacity": "50 req/min", "estimated_usage": "6 req/min" }
  ],
  "command": {                                  # required
    "mode": "shell",                            #   "shell" or "exec"
    "shell": "echo hello",                      #   required if mode=shell
    "workingDirectory": "/path"
  },
  "wake": {                                     # optional
    "enabled": true,
    "action": "wake",
    "leadMinutes": 2                            #   NOT "minutes_before"
  },
  "until": {                                    # optional — action per outcome
    "on_success": "delete",                      #   "delete" or "continue" (required if until present)
    "on_failure": "continue"                     #   "delete" or "continue" (required if until present)
  },
  "notifications": {                            # optional
    "onSuccess": false,
    "onFailure": true,
    "slackActivity": true                       #   false = no Slack lifecycle thread/messages
  },
  "callback": {                                 # optional — reconnect results to terminal
    "type": "cmux",                             #   "cmux", "ghostty", or "wezterm"
    "workspace_id": "workspace-uuid",           #   cmux workspace identity
    "surface_id": "surface-uuid",               #   exact cmux surface identity
    "session_id": "abc-def-123",                #   Claude Code session ID used for identity/resume
    "amq": true                                 #   cmux+session defaults true at creation; false opts out
  }
}

Examples: ~/git/playground/wakelite/docs/*.timer.json""",
    )
    timer_create.add_argument("--file", required=True, help="json file describing timer payload")
    timer_create.add_argument("--idempotency-key", required=True)

    timer_update = timer_sub.add_parser("update", help="update timer fields from a JSON patch file or inline flags")
    timer_update.add_argument("timer_id")
    timer_update.add_argument("--file", help="json file with fields to patch")
    timer_update.add_argument("--idempotency-key", default=str(uuid4()))
    timer_update.add_argument("--name", help="patch top-level name")
    timer_update.add_argument("--time", help="patch recurrence.time (HH:MM)")
    timer_update.add_argument("--comment", help="patch top-level comment")
    timer_update.add_argument("--shell", help="patch command.shell")
    timer_update.add_argument("--enabled", type=_parse_bool, help="patch top-level enabled (true/false)")

    timer_enable = timer_sub.add_parser("enable")
    timer_enable.add_argument("timer_id")
    timer_enable.add_argument("--idempotency-key", required=True)

    timer_disable = timer_sub.add_parser("disable")
    timer_disable.add_argument("timer_id")
    timer_disable.add_argument("--idempotency-key", required=True)

    timer_delete = timer_sub.add_parser("delete")
    timer_delete.add_argument("timer_id")
    timer_delete.add_argument("--idempotency-key", required=True)

    timer_run_now = timer_sub.add_parser("run-now")
    timer_run_now.add_argument("timer_id")
    timer_run_now.add_argument("--idempotency-key", required=True)

    timer_clone = timer_sub.add_parser("clone", help="clone an existing timer")
    timer_clone.add_argument("timer_id")
    timer_clone.add_argument("--name", help="override timer name")
    timer_clone.add_argument("--patch-file", help="JSON file with override fields")
    timer_clone.add_argument("--idempotency-key", default=str(uuid4()))

    timer_from_tpl = timer_sub.add_parser("from-template", help="create timer from a template")
    timer_from_tpl.add_argument("template", help="template name (e.g. reminder, callback, health-check)")
    timer_from_tpl.add_argument("--name", required=True, help="timer name")
    timer_from_tpl.add_argument("--shell", help="shell command")
    timer_from_tpl.add_argument("--time", help="run time HH:MM")
    timer_from_tpl.add_argument("--date", help="date YYYY-MM-DD (for once timers)")
    timer_from_tpl.add_argument("--every", help="interval (e.g. 5m, 30s)")
    timer_from_tpl.add_argument("--comment", help="timer description")
    timer_from_tpl.add_argument("--idempotency-key", default=str(uuid4()))

    template = sub.add_parser("template", help="template operations")
    template_sub = template.add_subparsers(dest="template_cmd", required=True)
    template_sub.add_parser("list", help="list available templates")
    template_show = template_sub.add_parser("show", help="show template details")
    template_show.add_argument("name", help="template name")

    runs = sub.add_parser("runs", help="run history")
    runs_sub = runs.add_subparsers(dest="runs_cmd", required=True)
    runs_list = runs_sub.add_parser("list")
    runs_list.add_argument("--limit", type=int, default=100)
    runs_list.add_argument("--timer-id")

    runs_logs = runs_sub.add_parser("logs")
    runs_logs.add_argument("run_id")
    runs_abort = runs_sub.add_parser("abort")
    runs_abort.add_argument("run_id")
    runs_abort.add_argument("--idempotency-key", required=True)

    launchd = sub.add_parser("launchd", help="install/uninstall launchd plists")
    launchd_sub = launchd.add_subparsers(dest="launchd_cmd", required=True)

    launchd_install = launchd_sub.add_parser("install")
    launchd_install.add_argument("--scope", choices=["user", "system", "both"], default="user")
    launchd_install.add_argument("--load", action="store_true")

    launchd_uninstall = launchd_sub.add_parser("uninstall")
    launchd_uninstall.add_argument("--scope", choices=["user", "system", "both"], default="user")
    launchd_uninstall.add_argument("--unload", action="store_true")

    launchd_sub.add_parser("status")
    launchd_sub.add_parser("restart", help="restart the runner via launchctl kickstart -k")

    incidents = sub.add_parser("incidents", help="incidents")
    incidents_sub = incidents.add_subparsers(dest="inc_cmd", required=True)
    incidents_list = incidents_sub.add_parser("list")
    incidents_list.add_argument("--limit", type=int, default=200)
    incidents_list.add_argument("--include-acked", action="store_true")

    incidents_ack = incidents_sub.add_parser("ack")
    incidents_ack.add_argument("incident_id", type=int)
    incidents_ack.add_argument("--idempotency-key", required=True)

    args = parser.parse_args()

    if args.cmd == "serve":
        # Reuse runner parser by replacing argv.
        sys.argv = [sys.argv[0], "--tick-seconds", str(args.tick_seconds)] + (["--with-mcp-http"] if args.with_mcp_http else [])
        runner_main()
        return

    if args.cmd == "reconcile":
        sys.argv = [sys.argv[0]] + (["--once"] if args.once else []) + (["--dry-run"] if args.dry_run else []) + ["--interval", str(args.interval)]
        reconciler_main()
        return

    if args.cmd == "manifest":
        _cmd_manifest(args)
        return

    if args.cmd == "mcp":
        if args.mcp_cmd == "install":
            _cmd_mcp_install(args)
            return
        if args.mcp_cmd == "print-config":
            _cmd_mcp_print(args)
            return

    if args.cmd == "health":
        _cmd_health(args)
        return

    if args.cmd == "timer":
        if args.timer_cmd == "list":
            _cmd_timer_list(args)
            return
        if args.timer_cmd == "create":
            _cmd_timer_create(args)
            return
        if args.timer_cmd == "update":
            _cmd_timer_update(args)
            return
        if args.timer_cmd == "delete":
            _cmd_timer_delete(args)
            return
        if args.timer_cmd == "enable":
            _cmd_timer_enable_disable(args, True)
            return
        if args.timer_cmd == "disable":
            _cmd_timer_enable_disable(args, False)
            return
        if args.timer_cmd == "run-now":
            _cmd_timer_run_now(args)
            return
        if args.timer_cmd == "clone":
            _cmd_timer_clone(args)
            return
        if args.timer_cmd == "from-template":
            _cmd_timer_from_template(args)
            return

    if args.cmd == "template":
        if args.template_cmd == "list":
            _cmd_template_list(args)
            return
        if args.template_cmd == "show":
            _cmd_template_show(args)
            return

    if args.cmd == "runs":
        if args.runs_cmd == "list":
            _cmd_runs_list(args)
            return
        if args.runs_cmd == "logs":
            _cmd_runs_logs(args)
            return
        if args.runs_cmd == "abort":
            _cmd_runs_abort(args)
            return

    if args.cmd == "launchd":
        if args.launchd_cmd == "install":
            _cmd_launchd_install(args)
            return
        if args.launchd_cmd == "uninstall":
            _cmd_launchd_uninstall(args)
            return
        if args.launchd_cmd == "status":
            _cmd_launchd_status(args)
            return
        if args.launchd_cmd == "restart":
            _cmd_launchd_restart(args)
            return

    if args.cmd == "incidents":
        if args.inc_cmd == "list":
            _cmd_incidents_list(args)
            return
        if args.inc_cmd == "ack":
            _cmd_incidents_ack(args)
            return


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
