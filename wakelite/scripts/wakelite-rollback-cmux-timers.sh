#!/usr/bin/env bash
set -euo pipefail

# CC-95 HIGH#6: resolve WAKELITECTL by precedence rather than hardcoding a
# user-specific install path. Order:
#   1. $WAKELITECTL env override (caller forces a specific binary). A
#      non-executable override is a hard error — we do NOT silently fall
#      through to other candidates because the caller's intent was explicit.
#   2. <script_dir>/../../bin/wakelitectl  — the canonical repo layout. The
#      script lives at <repo>/wakelite/scripts/, the binary at <repo>/bin/,
#      so two parent-hops resolve correctly. Authoritative when run in-place
#      from a checkout.
#   3. `command -v wakelitectl`            — installed on PATH (e.g. via a
#      symlink or shim).
# Each candidate must exist AND be executable. A missing binary fails at
# resolve time with a clear message rather than masquerading as an unrelated
# subprocess failure later.
__resolve_wakelitectl() {
  # Errors print directly from this function. The caller treats any non-zero
  # exit as terminal — no second-pass error rendering. (Earlier two-tier
  # design hit a bash quirk: $? inside `then` of `if ! cmd` is 0 because of
  # the `!` operator's own exit, so the rc-discrimination silently failed.)
  #
  # On success this function ALSO emits a diagnostic line to stderr naming
  # which precedence tier won (env-override / repo-relative / path). For
  # incident postmortems on emergency cmux rollback, knowing whether the
  # script ran a stale PATH binary vs the live repo binary is the difference
  # between "rollback worked but applied the wrong logic" and "rollback ran
  # what we wanted." (CC-95 MEDIUM, claude-nyx + claude-artemis converged.)
  if [[ -n "${WAKELITECTL:-}" ]]; then
    if [[ -x "$WAKELITECTL" ]]; then
      printf 'wakelite-rollback: using wakelitectl=%s (tier=env-override)\n' "$WAKELITECTL" >&2
      printf '%s' "$WAKELITECTL"
      return 0
    fi
    printf 'error: WAKELITECTL=%s is not executable\n' "$WAKELITECTL" >&2
    return 1
  fi

  # Resolve the directory containing THIS script, even if invoked via a
  # symlink. Avoids `realpath` (not available on stock macOS bash 3.2).
  local script_path script_dir
  script_path="${BASH_SOURCE[0]}"
  while [[ -L "$script_path" ]]; do
    local link_target
    link_target="$(readlink "$script_path")"
    if [[ "$link_target" == /* ]]; then
      script_path="$link_target"
    else
      script_path="$(cd "$(dirname "$script_path")" && pwd)/$link_target"
    fi
  done
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"

  local candidate
  candidate="$script_dir/../../bin/wakelitectl"
  if [[ -x "$candidate" ]]; then
    # Normalize away the ../../ for cleaner error messages later.
    candidate="$(cd "$(dirname "$candidate")" && pwd)/$(basename "$candidate")"
    printf 'wakelite-rollback: using wakelitectl=%s (tier=repo-relative)\n' "$candidate" >&2
    printf '%s' "$candidate"
    return 0
  fi

  if candidate="$(command -v wakelitectl 2>/dev/null)"; then
    printf 'wakelite-rollback: using wakelitectl=%s (tier=path)\n' "$candidate" >&2
    printf '%s' "$candidate"
    return 0
  fi

  printf 'error: could not locate wakelitectl. Set $WAKELITECTL, install it on PATH, or run this script from a checkout so <script_dir>/../../bin/wakelitectl resolves.\n' >&2
  return 1
}

WAKELITECTL="$(__resolve_wakelitectl)" || exit 1

TIMER_FILE="${WAKELITE_TIMER_FILE:-${WAKELITE_HOME:-$HOME/.wakelite}/timers.json}"

usage() {
  printf 'usage: %s --list | --neutralize | --rewrite ghostty|wezterm\n' "$0" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_jq() {
  command -v jq >/dev/null 2>&1 || die "jq is required"
}

load_timers_json() {
  local output
  if output="$("$WAKELITECTL" timer list 2>/dev/null)"; then
    printf '%s\n' "$output"
    return 0
  fi

  if [[ -r "$TIMER_FILE" ]]; then
    printf 'warning: wakelitectl timer list failed; reading %s\n' "$TIMER_FILE" >&2
    cat "$TIMER_FILE"
    return 0
  fi

  printf 'warning: wakelitectl timer list failed and no timer file was readable; assuming no cmux timers\n' >&2
  printf '{"timers":[]}\n'
}

cmux_timers_json() {
  # NOTE: `(.enabled // true) == true` is BROKEN — jq's // alternative
  # operator returns the alternative when the LHS is null OR FALSE, so
  # `false // true` evaluates to `true` and explicitly-disabled timers
  # leak through. Use `(.enabled != false)` instead: missing/null both
  # default to enabled (treated as != false), explicit `false` filters
  # out. Caught by tests/test_rollback_cmux_timers.sh case 04.
  load_timers_json | jq '
    def timers:
      if type == "object" and has("timers") then .timers
      elif type == "array" then .
      else []
      end;
    [timers[]? | select((.enabled != false) and ((.callback // {}).type == "cmux"))]
  '
}

require_wakelitectl() {
  [[ -x "$WAKELITECTL" ]] || die "wakelitectl not executable at $WAKELITECTL"
}

update_timer_callback() {
  local timer_id="$1"
  local patch_file="$2"
  local key="rollback-cmux-${timer_id}-$(date +%s)"
  "$WAKELITECTL" timer update "$timer_id" --file "$patch_file" --idempotency-key "$key" >/dev/null
}

neutralize() {
  require_wakelitectl
  local timers
  timers="$(cmux_timers_json)"
  local count
  count="$(printf '%s\n' "$timers" | jq 'length')"
  if [[ "$count" == "0" ]]; then
    printf 'No active cmux timers found.\n'
    return 0
  fi

  # NOTE: `trap 'rm -f "$patch"' RETURN` was previously used here, but the
  # RETURN trap fires AFTER the function's locals go out of scope in some
  # bash invocation contexts (subshells from `bash script.sh` vs sourced),
  # producing a `set -u` "patch: unbound variable" error. Earlier fix used
  # inline cleanup at every exit path, but `set -euo pipefail` causes the
  # function to exit BEFORE the trailing `rm -f` if `update_timer_callback`
  # fails — leaking the tmpfile. CC-95 MEDIUM (5/5 engines): switch to
  # function-local `trap EXIT` with `${patch:-}` for set-u safety. EXIT
  # fires regardless of how the function terminates; locals at that point
  # are still in scope because the trap is installed BEFORE local
  # assignment, but `${patch:-}` parameter expansion is set-u-safe even
  # if the trap fires before the assignment lands.
  local patch=""
  trap 'rm -f "${patch:-}"' EXIT
  patch="$(mktemp)"
  printf '{"callback":null}\n' >"$patch"

  printf '%s\n' "$timers" | jq -r '.[].id' | while IFS= read -r timer_id; do
    [[ -n "$timer_id" ]] || continue
    update_timer_callback "$timer_id" "$patch"
    printf 'Neutralized cmux callback for timer %s\n' "$timer_id"
  done

  rm -f "$patch"
  trap - EXIT
}

rewrite() {
  require_wakelitectl
  local target="$1"
  local timers
  timers="$(cmux_timers_json)"
  local count
  count="$(printf '%s\n' "$timers" | jq 'length')"
  if [[ "$count" == "0" ]]; then
    printf 'No active cmux timers found.\n'
    return 0
  fi

  # See neutralize() for why we use `trap EXIT` (with ${patch:-} for set-u
  # safety) instead of `trap RETURN` or pure inline cleanup. Run target
  # validation BEFORE creating the tmpfile so a die() doesn't leak it.
  case "$target" in
    ghostty)
      [[ -n "${GHOSTTY_TERMINAL_ID:-}" ]] || die "--rewrite ghostty requires GHOSTTY_TERMINAL_ID"
      ;;
    wezterm)
      [[ -n "${WEZTERM_PANE:-}" && "$WEZTERM_PANE" =~ ^[0-9]+$ ]] || die "--rewrite wezterm requires numeric WEZTERM_PANE"
      ;;
    *)
      die "--rewrite target must be ghostty or wezterm"
      ;;
  esac

  local patch=""
  trap 'rm -f "${patch:-}"' EXIT
  patch="$(mktemp)"

  printf '%s\n' "$timers" | jq -c '.[] | {id, session_id: (.callback.session_id // "")}' | while IFS= read -r row; do
    local timer_id session_id
    timer_id="$(printf '%s\n' "$row" | jq -r '.id')"
    session_id="$(printf '%s\n' "$row" | jq -r '.session_id')"
    if [[ "$target" == "ghostty" ]]; then
      jq -n --arg terminal_id "$GHOSTTY_TERMINAL_ID" --arg session_id "$session_id" '
        {callback:{type:"ghostty", terminal_id:$terminal_id}}
        | if $session_id != "" then .callback.session_id = $session_id else . end
      ' >"$patch"
    else
      jq -n --argjson pane_id "$WEZTERM_PANE" --arg session_id "$session_id" '
        {callback:{type:"wezterm", pane_id:$pane_id}}
        | if $session_id != "" then .callback.session_id = $session_id else . end
      ' >"$patch"
    fi
    update_timer_callback "$timer_id" "$patch"
    printf 'Rewrote cmux callback for timer %s to %s\n' "$timer_id" "$target"
  done

  rm -f "$patch"
  trap - EXIT
}

main() {
  require_jq
  case "${1:-}" in
    --list)
      cmux_timers_json
      ;;
    --neutralize)
      neutralize
      ;;
    --rewrite)
      [[ $# -eq 2 ]] || { usage; exit 2; }
      rewrite "$2"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
