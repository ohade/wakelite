#!/usr/bin/env bash
set -euo pipefail

WAKELITECTL="${WAKELITECTL:-$HOME/git/playground/wakelite/bin/wakelitectl}"
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
  load_timers_json | jq '
    def timers:
      if type == "object" and has("timers") then .timers
      elif type == "array" then .
      else []
      end;
    [timers[]? | select((.enabled // true) == true and ((.callback // {}).type == "cmux"))]
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

  local patch
  patch="$(mktemp)"
  trap 'rm -f "$patch"' RETURN
  printf '{"callback":null}\n' >"$patch"

  printf '%s\n' "$timers" | jq -r '.[].id' | while IFS= read -r timer_id; do
    [[ -n "$timer_id" ]] || continue
    update_timer_callback "$timer_id" "$patch"
    printf 'Neutralized cmux callback for timer %s\n' "$timer_id"
  done
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

  local patch
  patch="$(mktemp)"
  trap 'rm -f "$patch"' RETURN

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
