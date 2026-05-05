#!/usr/bin/env bash
# CC-95 HIGH#7: Test harness for wakelite/scripts/wakelite-rollback-cmux-timers.sh.
#
# Exercises the three modes (--list, --neutralize, --rewrite ghostty|wezterm)
# plus the WAKELITECTL resolution chain (CC-95 HIGH#6) and error paths.
#
# Strategy: mock `wakelitectl` with a bash script that (a) records its argv +
# cwd to a log file, (b) prints fixture JSON for `timer list`, and (c) reads
# the patch file passed to `timer update` and copies it to a "captured patch"
# location. Each test runs in a fresh tmpdir so logs don't leak between cases.
#
# Run: bash tests/test_rollback_cmux_timers.sh
# Exit 0 = all pass; exit 1 = at least one failed.

set -uo pipefail  # NOT -e: we want to keep running and report all failures.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/wakelite/scripts/wakelite-rollback-cmux-timers.sh"

if [[ ! -x "$SCRIPT" ]]; then
  printf 'fatal: rollback script not executable at %s\n' "$SCRIPT" >&2
  exit 1
fi

# ── Test runner state ──
PASSED=0
FAILED=0
FAILED_TESTS=()
CURRENT=""

start_test() {
  CURRENT="$1"
  printf '── %s\n' "$CURRENT"
}

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    printf '   OK   %s\n' "$label"
    return 0
  fi
  printf '   FAIL %s\n        expected: %q\n        actual:   %q\n' "$label" "$expected" "$actual"
  return 1
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    printf '   OK   %s\n' "$label"
    return 0
  fi
  printf '   FAIL %s\n        expected substring: %q\n        in: %q\n' "$label" "$needle" "$haystack"
  return 1
}

assert_json_eq() {
  local label="$1" expected_jq="$2" actual_json="$3" expected_value="$4"
  local got
  got="$(printf '%s' "$actual_json" | jq -r "$expected_jq" 2>/dev/null || echo "JQ_PARSE_FAIL")"
  if [[ "$got" == "$expected_value" ]]; then
    printf '   OK   %s\n' "$label"
    return 0
  fi
  printf '   FAIL %s\n        jq: %s\n        expected: %q\n        actual:   %q\n        json: %s\n' "$label" "$expected_jq" "$expected_value" "$got" "$actual_json"
  return 1
}

end_test() {
  local rc=$1
  if [[ $rc -eq 0 ]]; then
    PASSED=$((PASSED + 1))
  else
    FAILED=$((FAILED + 1))
    FAILED_TESTS+=("$CURRENT")
  fi
}

# ── Mock wakelitectl factory ──
# Creates a mock `wakelitectl` script in $1 (which becomes $WAKELITECTL).
# The mock:
#   - Logs every invocation (argv + cwd) to $1.log
#   - For `timer list` (no args after), prints the JSON in $1.fixture
#   - For `timer update <id> --file <path> --idempotency-key <key>`, copies
#     <path> to $1.patches/<id>.json
make_mock_wakelitectl() {
  local mock_path="$1"
  local fixture_json="$2"
  printf '%s\n' "$fixture_json" > "${mock_path}.fixture"
  : > "${mock_path}.log"
  mkdir -p "${mock_path}.patches"
  cat > "$mock_path" <<'MOCK_EOF'
#!/usr/bin/env bash
set -uo pipefail
SELF="$0"
LOG="${SELF}.log"
FIXTURE="${SELF}.fixture"
PATCHES="${SELF}.patches"

printf 'argv=%s cwd=%s\n' "$*" "$(pwd)" >> "$LOG"

if [[ "${1:-}" == "timer" && "${2:-}" == "list" ]]; then
  cat "$FIXTURE"
  exit 0
fi

if [[ "${1:-}" == "timer" && "${2:-}" == "update" ]]; then
  TIMER_ID="${3:-}"
  PATCH_FILE=""
  for ((i=4; i<=$#; i++)); do
    if [[ "${!i}" == "--file" ]]; then
      next=$((i+1))
      PATCH_FILE="${!next}"
      break
    fi
  done
  if [[ -n "$TIMER_ID" && -n "$PATCH_FILE" && -f "$PATCH_FILE" ]]; then
    cp "$PATCH_FILE" "$PATCHES/${TIMER_ID}.json"
  fi
  printf 'updated %s\n' "$TIMER_ID"
  exit 0
fi

# Unknown invocation — fail loudly so tests notice.
printf 'mock-wakelitectl: unknown invocation: %s\n' "$*" >&2
exit 99
MOCK_EOF
  chmod +x "$mock_path"
}

# ── Fixture: typical timer list output (mixed callback types) ──
read -r -d '' MIXED_FIXTURE <<'EOF' || true
{"timers": [
  {"id": "t-cmux-01",    "name": "cmux-callback-1",    "enabled": true,  "callback": {"type": "cmux", "workspace_id": "ws-a", "surface_id": "sf-a", "session_id": "sess-1"}},
  {"id": "t-cmux-02",    "name": "cmux-callback-2",    "enabled": true,  "callback": {"type": "cmux", "workspace_id": "ws-b", "surface_id": "sf-b"}},
  {"id": "t-cmux-disabled", "name": "cmux-disabled",   "enabled": false, "callback": {"type": "cmux", "workspace_id": "ws-c", "surface_id": "sf-c"}},
  {"id": "t-ghostty",    "name": "ghostty-callback",   "enabled": true,  "callback": {"type": "ghostty", "terminal_id": "uuid-g"}},
  {"id": "t-wezterm",    "name": "wezterm-callback",   "enabled": true,  "callback": {"type": "wezterm", "pane_id": 7}},
  {"id": "t-no-callback","name": "no-callback",        "enabled": true}
]}
EOF

read -r -d '' EMPTY_FIXTURE <<'EOF' || true
{"timers": []}
EOF

# Some upstream wakelitectl versions return a top-level array instead of an
# object with a "timers" key. The script's jq handles both shapes — assert
# that explicitly with a separate fixture.
read -r -d '' ARRAY_FIXTURE <<'EOF' || true
[
  {"id": "t-cmux-array", "enabled": true, "callback": {"type": "cmux", "workspace_id": "ws-x", "surface_id": "sf-x"}}
]
EOF

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 01 — bash -n syntax check                                       ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "01 bash -n syntax check"
( bash -n "$SCRIPT" ) >/dev/null 2>&1
rc=$?
assert_eq "exit code" "0" "$rc" || { end_test 1; FAILED_FORCE=1; }
[[ "${FAILED_FORCE:-0}" -eq 1 ]] || end_test 0
unset FAILED_FORCE

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 02 — usage error on no args                                     ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "02 usage error on no args"
out=$(bash "$SCRIPT" 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "2" "$rc" || suite_rc=1
assert_contains "usage line" "usage:" "$out" || suite_rc=1
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 03 — usage error on unknown subcommand                          ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "03 unknown subcommand exits 2"
out=$(bash "$SCRIPT" --bogus 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "2" "$rc" || suite_rc=1
assert_contains "usage line on bad subcommand" "usage:" "$out" || suite_rc=1
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 04 — --list filters to enabled cmux timers only                 ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "04 --list filters to enabled cmux timers only"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --list 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
assert_json_eq "count of cmux entries" "length" "$out" "2" || suite_rc=1
assert_json_eq "first id" ".[0].id" "$out" "t-cmux-01" || suite_rc=1
assert_json_eq "second id" ".[1].id" "$out" "t-cmux-02" || suite_rc=1
# Disabled cmux NOT included
assert_json_eq "disabled excluded" '[.[].id] | contains(["t-cmux-disabled"])' "$out" "false" || suite_rc=1
# Non-cmux NOT included
assert_json_eq "ghostty excluded" '[.[].id] | contains(["t-ghostty"])' "$out" "false" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 05 — --list handles top-level-array fixture shape               ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "05 --list handles top-level-array fixture shape"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$ARRAY_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --list 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
assert_json_eq "count" "length" "$out" "1" || suite_rc=1
assert_json_eq "id" ".[0].id" "$out" "t-cmux-array" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 06 — --list returns empty when no cmux timers                   ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "06 --list returns empty when no cmux timers"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$EMPTY_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --list 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
assert_json_eq "count" "length" "$out" "0" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 07 — --list falls back to TIMER_FILE when wakelitectl fails     ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "07 --list falls back to WAKELITE_TIMER_FILE on wakelitectl failure"
TMP=$(mktemp -d)
# Mock wakelitectl that always fails
cat > "$TMP/wakelitectl" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$TMP/wakelitectl"
# Stage timer fixture as the timer file
printf '%s' "$MIXED_FIXTURE" > "$TMP/timers.json"
out=$(WAKELITECTL="$TMP/wakelitectl" WAKELITE_TIMER_FILE="$TMP/timers.json" bash "$SCRIPT" --list 2>&1)
rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
# Strip the leading warning line, parse the rest as JSON
clean=$(printf '%s\n' "$out" | grep -v '^warning:')
assert_json_eq "count" "length" "$clean" "2" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 08 — --list with no wakelitectl AND no timer file: empty result ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "08 --list degrades gracefully with no wakelitectl + no timer file"
TMP=$(mktemp -d)
# wakelitectl that always fails
cat > "$TMP/wakelitectl" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$TMP/wakelitectl"
out=$(WAKELITECTL="$TMP/wakelitectl" WAKELITE_TIMER_FILE="$TMP/does-not-exist.json" bash "$SCRIPT" --list 2>&1)
rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
clean=$(printf '%s\n' "$out" | grep -v '^warning:')
assert_json_eq "count" "length" "$clean" "0" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 09 — --neutralize rewrites every cmux timer's callback to null ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "09 --neutralize patches each cmux timer with callback:null"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --neutralize 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
# 2 cmux timers expected -> 2 update calls
update_calls=$( { grep -c '^argv=timer update' "$MOCK.log" 2>/dev/null; true; } | head -1 )
[[ -z "$update_calls" ]] && update_calls=0
assert_eq "update call count" "2" "$update_calls" || suite_rc=1
# Each captured patch must be {"callback": null}
for id in t-cmux-01 t-cmux-02; do
  if [[ -f "$MOCK.patches/$id.json" ]]; then
    content=$(cat "$MOCK.patches/$id.json")
    assert_json_eq "patch.$id has callback=null" '.callback' "$content" "null" || suite_rc=1
  else
    printf '   FAIL no patch file for %s\n' "$id"
    suite_rc=1
  fi
done
# Disabled cmux NOT touched
[[ -f "$MOCK.patches/t-cmux-disabled.json" ]] && { printf '   FAIL disabled cmux was touched\n'; suite_rc=1; } || printf '   OK   disabled cmux not touched\n'
# Non-cmux NOT touched
[[ -f "$MOCK.patches/t-ghostty.json" ]] && { printf '   FAIL ghostty was touched\n'; suite_rc=1; } || printf '   OK   ghostty not touched\n'
[[ -f "$MOCK.patches/t-wezterm.json" ]] && { printf '   FAIL wezterm was touched\n'; suite_rc=1; } || printf '   OK   wezterm not touched\n'
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 10 — --neutralize on empty store reports clean message         ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "10 --neutralize on empty store: clean noop message"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$EMPTY_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --neutralize 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
assert_contains "noop message" "No active cmux timers found" "$out" || suite_rc=1
update_calls=$( { grep -c '^argv=timer update' "$MOCK.log" 2>/dev/null; true; } | head -1 )
[[ -z "$update_calls" ]] && update_calls=0
assert_eq "no update calls" "0" "$update_calls" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 11 — --rewrite ghostty produces typed patch with terminal_id   ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "11 --rewrite ghostty patches each cmux with terminal_id"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(GHOSTTY_TERMINAL_ID="ghostty-uuid-zzz" WAKELITECTL="$MOCK" \
  bash "$SCRIPT" --rewrite ghostty 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
# t-cmux-01 has session_id, t-cmux-02 doesn't
if [[ -f "$MOCK.patches/t-cmux-01.json" ]]; then
  c=$(cat "$MOCK.patches/t-cmux-01.json")
  assert_json_eq "t-01 type"        '.callback.type'        "$c" "ghostty" || suite_rc=1
  assert_json_eq "t-01 terminal_id" '.callback.terminal_id' "$c" "ghostty-uuid-zzz" || suite_rc=1
  assert_json_eq "t-01 session_id"  '.callback.session_id'  "$c" "sess-1" || suite_rc=1
fi
if [[ -f "$MOCK.patches/t-cmux-02.json" ]]; then
  c=$(cat "$MOCK.patches/t-cmux-02.json")
  assert_json_eq "t-02 type"        '.callback.type'                "$c" "ghostty" || suite_rc=1
  assert_json_eq "t-02 terminal_id" '.callback.terminal_id'         "$c" "ghostty-uuid-zzz" || suite_rc=1
  # When source has no session_id, patch must NOT carry one (would be invalid).
  assert_json_eq "t-02 no session_id" '.callback | has("session_id")' "$c" "false" || suite_rc=1
fi
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 12 — --rewrite wezterm produces typed patch with pane_id (int) ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "12 --rewrite wezterm patches with numeric pane_id"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WEZTERM_PANE="42" WAKELITECTL="$MOCK" \
  bash "$SCRIPT" --rewrite wezterm 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
if [[ -f "$MOCK.patches/t-cmux-01.json" ]]; then
  c=$(cat "$MOCK.patches/t-cmux-01.json")
  assert_json_eq "type"     '.callback.type'           "$c" "wezterm" || suite_rc=1
  assert_json_eq "pane_id"  '.callback.pane_id'        "$c" "42" || suite_rc=1
  assert_json_eq "pane num" '.callback.pane_id | type' "$c" "number" || suite_rc=1
fi
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 13 — --rewrite ghostty without GHOSTTY_TERMINAL_ID errors out  ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "13 --rewrite ghostty requires GHOSTTY_TERMINAL_ID"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
# Strip GHOSTTY_TERMINAL_ID if currently in the env
out=$(env -u GHOSTTY_TERMINAL_ID WAKELITECTL="$MOCK" \
  bash "$SCRIPT" --rewrite ghostty 2>&1); rc=$?
suite_rc=0
assert_contains "error message names env var" "GHOSTTY_TERMINAL_ID" "$out" || suite_rc=1
[[ "$rc" -ne 0 ]] && printf '   OK   non-zero rc (%d)\n' "$rc" || { printf '   FAIL expected non-zero rc, got 0\n'; suite_rc=1; }
update_calls=$( { grep -c '^argv=timer update' "$MOCK.log" 2>/dev/null; true; } | head -1 )
[[ -z "$update_calls" ]] && update_calls=0
assert_eq "no update calls" "0" "$update_calls" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 14 — --rewrite wezterm rejects non-numeric WEZTERM_PANE        ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "14 --rewrite wezterm rejects non-numeric WEZTERM_PANE"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WEZTERM_PANE="not-a-number" WAKELITECTL="$MOCK" \
  bash "$SCRIPT" --rewrite wezterm 2>&1); rc=$?
suite_rc=0
[[ "$rc" -ne 0 ]] && printf '   OK   non-zero rc (%d)\n' "$rc" || { printf '   FAIL expected non-zero rc\n'; suite_rc=1; }
assert_contains "error names WEZTERM_PANE" "WEZTERM_PANE" "$out" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 15 — --rewrite kitty (unknown target) errors out               ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "15 --rewrite kitty (unknown target) rejected"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --rewrite kitty 2>&1); rc=$?
suite_rc=0
[[ "$rc" -ne 0 ]] && printf '   OK   non-zero rc (%d)\n' "$rc" || { printf '   FAIL expected non-zero rc\n'; suite_rc=1; }
assert_contains "error explains valid targets" "ghostty" "$out" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 16 — --rewrite missing target argument errors out              ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "16 --rewrite without target arg exits 2"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$MIXED_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --rewrite 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "2" "$rc" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 17 — WAKELITECTL resolution: explicit env override is honored  ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "17 resolution: explicit WAKELITECTL env override"
TMP=$(mktemp -d); MOCK="$TMP/wakelitectl"
make_mock_wakelitectl "$MOCK" "$EMPTY_FIXTURE"
out=$(WAKELITECTL="$MOCK" bash "$SCRIPT" --list 2>&1); rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
# Verify the mock was actually invoked (log file got a line)
log_lines=$(wc -l < "$MOCK.log" | tr -d ' ')
[[ "$log_lines" -ge 1 ]] && printf '   OK   mock invoked (log lines=%s)\n' "$log_lines" || { printf '   FAIL mock not invoked\n'; suite_rc=1; }
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 18 — WAKELITECTL resolution: non-executable override fails     ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "18 resolution: non-executable WAKELITECTL override errors"
out=$(WAKELITECTL="/this/path/definitely/does/not/exist" bash "$SCRIPT" --list 2>&1)
rc=$?
suite_rc=0
assert_eq "rc" "1" "$rc" || suite_rc=1
assert_contains "error names override" "/this/path/definitely/does/not/exist" "$out" || suite_rc=1
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 19 — WAKELITECTL resolution: PATH fallback works               ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "19 resolution: PATH fallback discovers wakelitectl"
TMP=$(mktemp -d); MOCK_DIR="$TMP/bin"; mkdir -p "$MOCK_DIR"
make_mock_wakelitectl "$MOCK_DIR/wakelitectl" "$EMPTY_FIXTURE"
# Copy the script to a place where ../../bin/ does NOT contain wakelitectl,
# so PATH must be the resolution path that succeeds.
SCRIPT_COPY="$TMP/standalone-rollback.sh"
cp "$SCRIPT" "$SCRIPT_COPY"; chmod +x "$SCRIPT_COPY"
out=$(env -i HOME="$HOME" PATH="$MOCK_DIR:/usr/bin:/bin" bash "$SCRIPT_COPY" --list 2>&1)
rc=$?
suite_rc=0
assert_eq "rc" "0" "$rc" || suite_rc=1
assert_json_eq "count" "length" "$out" "0" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ TEST 20 — WAKELITECTL resolution: nothing found yields clear error  ║
# ╚══════════════════════════════════════════════════════════════════════╝
start_test "20 resolution: no candidates anywhere prints remediation hint"
TMP=$(mktemp -d)
SCRIPT_COPY="$TMP/standalone-rollback.sh"
cp "$SCRIPT" "$SCRIPT_COPY"; chmod +x "$SCRIPT_COPY"
# env -i with PATH that has no wakelitectl, no script_dir/../../bin
out=$(env -i HOME="$HOME" PATH="/usr/bin:/bin" bash "$SCRIPT_COPY" --list 2>&1)
rc=$?
suite_rc=0
assert_eq "rc" "1" "$rc" || suite_rc=1
assert_contains "names WAKELITECTL"  "WAKELITECTL"        "$out" || suite_rc=1
assert_contains "names PATH option"  "PATH"               "$out" || suite_rc=1
rm -rf "$TMP"
end_test $suite_rc

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ Summary                                                              ║
# ╚══════════════════════════════════════════════════════════════════════╝
printf '\n'
printf '═══════════════════════════════════════════════════════════════════\n'
printf 'SUMMARY: %d passed, %d failed (total %d)\n' "$PASSED" "$FAILED" "$((PASSED + FAILED))"
if [[ "$FAILED" -gt 0 ]]; then
  printf 'Failed tests:\n'
  for t in "${FAILED_TESTS[@]}"; do
    printf '  - %s\n' "$t"
  done
  exit 1
fi
exit 0
