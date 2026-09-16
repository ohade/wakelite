#!/usr/bin/env bash
# daily-merge-stable.sh — Merge a moving integration ref into owned worktrees.
#
# Routine failures (for example, a temporary network outage) are retried by the
# next scheduled run and return EX_TEMPFAIL (75). Immediate-action failures and
# a configurable same-branch failure streak are escalated to Slack. Successful
# runs and successfully repaired non-fast-forward pushes are Slack-silent.
set -uo pipefail

PRODUCTS_REPO="${DAILY_MERGE_PRODUCTS_REPO:-}"
WORKTREE_DIR="${DAILY_MERGE_WORKTREE_DIR:-}"
MERGE_REF="${DAILY_MERGE_REF:-}"
STATE_DIR="${DAILY_MERGE_STATE_DIR:-${HOME}/.local/state/daily-merge-stable}"
LOG_FILE="${DAILY_MERGE_LOG_FILE:-/tmp/daily-merge-stable-$(date '+%Y%m%d').log}"
SLACK_CHANNEL="${DAILY_MERGE_SLACK_CHANNEL:-}"
STREAK_THRESHOLD="${DAILY_MERGE_STREAK_THRESHOLD:-3}"
CLAUDE_IDLE_TIMEOUT="${DAILY_MERGE_CLAUDE_IDLE_TIMEOUT:-120}"
CLAUDE_MAX_TIMEOUT="${DAILY_MERGE_CLAUDE_MAX_TIMEOUT:-900}"
GIT_BIN="${DAILY_MERGE_GIT_BIN:-git}"
CLAUDE_BIN="${DAILY_MERGE_CLAUDE_BIN:-claude}"
PYTHON_BIN="${DAILY_MERGE_PYTHON_BIN:-python3}"
NOTIFIER_BIN="${DAILY_MERGE_NOTIFIER_BIN:-}"
WAKELITE_REPO="${DAILY_MERGE_WAKELITE_REPO:-}"
BYPASS_FILE="${DAILY_MERGE_BYPASS_FILE:-}"

# Git ref names cannot contain colons, so a colon-delimited environment value
# safely carries deployment-specific temporary exclusions without baking them
# into this reusable script.
declare -a EXCLUDED_BRANCHES=()
if [[ -n "${DAILY_MERGE_EXCLUDED_BRANCHES:-}" ]]; then
    IFS=':' read -r -a EXCLUDED_BRANCHES <<<"$DAILY_MERGE_EXCLUDED_BRANCHES"
fi

merged=0
pushed=0
skipped=0
conflict_resolved=0
conflict_failed=0
already_uptodate=0
failure_count=0
had_failures=false
has_active_escalation=false
declare -a summary_lines=()
declare -a escalation_branches=()
declare -a escalation_classes=()
declare -a escalation_counts=()
declare -a escalation_details=()
declare -a escalation_state_files=()

REMOTE_SNAPSHOT=""

log() {
    local line
    line="[$(date '+%H:%M:%S')] $*"
    printf '%s\n' "$line" | tee -a "$LOG_FILE"
}

add_summary() {
    summary_lines+=("$1")
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    if [[ -n "$REMOTE_SNAPSHOT" && -f "$REMOTE_SNAPSHOT" ]]; then
        rm -f "$REMOTE_SNAPSHOT"
    fi
}
trap cleanup EXIT

is_excluded_branch() {
    local candidate="$1"
    local excluded
    for excluded in "${EXCLUDED_BRANCHES[@]}"; do
        if [[ "$candidate" == "$excluded" ]]; then
            return 0
        fi
    done
    return 1
}

state_file_for_branch() {
    local branch="$1"
    local key
    key="$(printf '%s' "$branch" | cksum | awk '{print $1 "-" $2}')"
    printf '%s/%s.state\n' "$STATE_DIR" "$key"
}

persist_state() {
    local state_file="$1"
    local branch="$2"
    local count="$3"
    local class="$4"
    local alerted="$5"
    local temp_file="${state_file}.tmp.$$"

    if ! printf '%s\n%s\n%s\n%s\n' "$branch" "$count" "$class" "$alerted" >"$temp_file"; then
        rm -f "$temp_file" 2>/dev/null || true
        return 1
    fi
    if ! mv "$temp_file" "$state_file"; then
        rm -f "$temp_file" 2>/dev/null || true
        return 1
    fi
}

queue_escalation() {
    escalation_branches+=("$1")
    escalation_classes+=("$2")
    escalation_counts+=("$3")
    escalation_details+=("$4")
    escalation_state_files+=("$5")
}

record_failure() {
    local branch="$1"
    local class="$2"
    local detail="$3"
    local immediate="$4"
    local state_file stored_branch stored_count stored_alerted count

    had_failures=true
    ((failure_count++))
    state_file="$(state_file_for_branch "$branch")"
    stored_branch=""
    stored_count=0
    stored_alerted=0

    if [[ -f "$state_file" ]]; then
        stored_branch="$(sed -n '1p' "$state_file" 2>/dev/null || true)"
        stored_count="$(sed -n '2p' "$state_file" 2>/dev/null || true)"
        stored_alerted="$(sed -n '4p' "$state_file" 2>/dev/null || true)"
        if [[ "$stored_branch" != "$branch" ]]; then
            stored_count=0
            stored_alerted=0
            immediate=true
            class="environment_fault"
            detail="failure-state key collision"
        fi
    fi

    case "$stored_count" in
        ''|*[!0-9]*) stored_count=0 ;;
    esac
    case "$stored_alerted" in
        0|1) ;;
        *) stored_alerted=0 ;;
    esac

    count=$((stored_count + 1))
    if ! persist_state "$state_file" "$branch" "$count" "$class" "$stored_alerted"; then
        log "ERROR: cannot persist failure state for $branch in $STATE_DIR"
        queue_escalation "$branch" "environment_fault" "$count" \
            "cannot persist branch failure state" ""
        has_active_escalation=true
        return
    fi

    log "Failure classified: branch=$branch class=$class streak=$count immediate=$immediate"
    if [[ "$stored_alerted" == "1" ]]; then
        has_active_escalation=true
    elif [[ "$immediate" == "true" || "$count" -ge "$STREAK_THRESHOLD" ]]; then
        queue_escalation "$branch" "$class" "$count" "$detail" "$state_file"
        has_active_escalation=true
    fi
}

mark_state_alerted() {
    local state_file="$1"
    local branch count class
    [[ -n "$state_file" && -f "$state_file" ]] || return 0
    branch="$(sed -n '1p' "$state_file" 2>/dev/null || true)"
    count="$(sed -n '2p' "$state_file" 2>/dev/null || true)"
    class="$(sed -n '3p' "$state_file" 2>/dev/null || true)"
    persist_state "$state_file" "$branch" "$count" "$class" 1
}

reset_failure() {
    local branch="$1"
    local state_file
    state_file="$(state_file_for_branch "$branch")"
    if [[ -f "$state_file" ]]; then
        rm -f "$state_file"
        log "Cleared failure streak for $branch"
    fi
}

classify_failure() {
    local output="$1"
    FAILURE_CLASS="unknown"
    FAILURE_IMMEDIATE=false

    if grep -Eqi \
        'No space left on device|disk quota exceeded|read-only file system' <<<"$output"; then
        FAILURE_CLASS="environment_fault"
        FAILURE_IMMEDIATE=true
    elif grep -Eqi \
        'Permission denied|authentication failed|authorization failed|access denied|could not read Username|invalid credentials|expired credentials|HTTP 40[13]' <<<"$output"; then
        FAILURE_CLASS="auth_permission"
        FAILURE_IMMEDIATE=true
    elif grep -Eqi \
        'repository not found|remote ref does not exist|does not appear to be a git repository|no such repository' <<<"$output"; then
        FAILURE_CLASS="remote_gone"
        FAILURE_IMMEDIATE=true
    elif grep -Eqi \
        'non-fast-forward|fetch first|tip of your current branch is behind' <<<"$output"; then
        FAILURE_CLASS="non_fast_forward"
        FAILURE_IMMEDIATE=false
    elif grep -Eqi \
        'pre-receive hook declined|protected branch|remote rejected|failed to push some refs.*hook|GH006' <<<"$output"; then
        FAILURE_CLASS="server_side_rejection"
        FAILURE_IMMEDIATE=true
    elif grep -Eqi \
        'Could not resolve (host|hostname)|operation timed out|connection timed out|network is unreachable|connection refused|connection reset|temporary failure in name resolution|VPN' <<<"$output"; then
        FAILURE_CLASS="transient_network"
        FAILURE_IMMEDIATE=false
    fi
}

# Auto-merge is only safe for branches you wrote yourself, so the caller
# declares who "yourself" is. DAILY_MERGE_AUTHOR_PATTERN is a case-insensitive
# extended regex matched against each commit's author name. Unset means no
# author is trusted, which makes every branch escalate instead of merging --
# the safe default for a fresh clone.
all_authors_are_trusted() {
    local authors="$1"
    local author
    local pattern="${DAILY_MERGE_AUTHOR_PATTERN:-}"
    if [[ -z "$pattern" ]]; then
        FOREIGN_AUTHOR="$(head -n1 <<<"$authors")"
        [[ -z "$FOREIGN_AUTHOR" ]] && return 0
        return 1
    fi
    local rc=0
    shopt -s nocasematch
    while IFS= read -r author; do
        [[ -z "$author" ]] && continue
        if [[ ! "$author" =~ $pattern ]]; then
            FOREIGN_AUTHOR="$author"
            rc=1
            break
        fi
    done <<<"$authors"
    shopt -u nocasematch
    return $rc
}

snapshot_sha_for_ref() {
    local ref="$1"
    [[ -n "$REMOTE_SNAPSHOT" && -f "$REMOTE_SNAPSHOT" ]] || return 0
    awk -v wanted="$ref" '$1 == wanted {print $2; exit}' "$REMOTE_SNAPSHOT"
}

remote_was_rewritten() {
    local wt_path="$1"
    local branch="$2"
    local previous_sha current_sha
    previous_sha="$(snapshot_sha_for_ref "origin/$branch")"
    current_sha="$($GIT_BIN -C "$wt_path" rev-parse "origin/$branch" 2>/dev/null || true)"
    [[ -n "$previous_sha" && -n "$current_sha" && "$previous_sha" != "$current_sha" ]] || return 1
    if ! "$GIT_BIN" -C "$wt_path" merge-base --is-ancestor "$previous_sha" "$current_sha" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

validate_remote_ownership() {
    local wt_path="$1"
    local branch="$2"
    local authors
    authors="$($GIT_BIN -C "$wt_path" log "HEAD..origin/$branch" --no-merges --format='%an' 2>/dev/null || true)"
    FOREIGN_AUTHOR=""
    if [[ -n "$authors" ]] && ! all_authors_are_trusted "$authors"; then
        record_failure "$branch" "ownership_change" \
            "remote-only non-merge commit is authored by ${FOREIGN_AUTHOR:-another owner}" true
        add_summary "ESCALATE (ownership changed): $(basename "$wt_path") [$branch]"
        return 1
    fi
    return 0
}

resolve_conflict_with_claude() {
    local wt_path="$1"
    local dir_name="$2"
    local branch="$3"
    local source_ref="$4"
    local claude_prompt claude_pid start_time last_activity last_mtime
    local now elapsed current_mtime idle exit_code timeout_reason

    log "  Merge conflict in $dir_name from $source_ref. Invoking Claude (idle=${CLAUDE_IDLE_TIMEOUT}s, max=${CLAUDE_MAX_TIMEOUT}s)..."
    unset ANTHROPIC_API_KEY 2>/dev/null || true
    claude_prompt="Resolve all merge conflicts in this git worktree. The merge is from ${source_ref} into the feature branch ${branch}. Resolve conflicts favoring the feature branch changes where intent is clear, otherwise keep both sides. After resolving ALL conflicts, stage all files with git add and commit the merge with git commit --no-edit. Do not push."

    (cd "$wt_path" && "$CLAUDE_BIN" -p "$claude_prompt" \
        --allowedTools "Bash(git*) Bash(find*) Read Grep Edit" \
        --max-turns 20 \
        --model sonnet) </dev/null >>"$LOG_FILE" 2>&1 &
    claude_pid=$!
    start_time=$(date +%s)
    last_activity=$start_time
    last_mtime=$(find "$wt_path" -maxdepth 3 -newer "$LOG_FILE" -type f 2>/dev/null | wc -l | tr -d ' ')
    timeout_reason=""

    while kill -0 "$claude_pid" 2>/dev/null; do
        sleep 10
        now=$(date +%s)
        elapsed=$((now - start_time))
        if [[ "$elapsed" -ge "$CLAUDE_MAX_TIMEOUT" ]]; then
            timeout_reason="hard timeout"
            kill "$claude_pid" 2>/dev/null || true
            break
        fi
        current_mtime=$(find "$wt_path" -maxdepth 3 -newer "$LOG_FILE" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$current_mtime" != "$last_mtime" ]]; then
            last_activity=$now
            last_mtime=$current_mtime
        fi
        idle=$((now - last_activity))
        if [[ "$idle" -ge "$CLAUDE_IDLE_TIMEOUT" ]]; then
            timeout_reason="idle timeout"
            kill "$claude_pid" 2>/dev/null || true
            break
        fi
    done

    wait "$claude_pid" 2>/dev/null
    exit_code=$?
    if [[ -n "$timeout_reason" ]]; then
        log "  Claude conflict resolution hit $timeout_reason on $dir_name"
        return 1
    fi
    if [[ "$exit_code" -ne 0 ]]; then
        log "  Claude conflict resolution exited $exit_code on $dir_name"
        return 1
    fi
    if ! "$GIT_BIN" -C "$wt_path" diff --check >/dev/null 2>&1 || \
       [[ -n "$($GIT_BIN -C "$wt_path" diff --name-only --diff-filter=U 2>/dev/null)" ]] || \
       "$GIT_BIN" -C "$wt_path" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
        log "  Claude returned without a complete, committed conflict resolution on $dir_name"
        return 1
    fi
    log "  Claude resolved and committed conflicts in $dir_name"
    ((conflict_resolved++))
    return 0
}

merge_source() {
    local wt_path="$1"
    local dir_name="$2"
    local branch="$3"
    local source_ref="$4"
    local merge_output merge_rc unresolved

    merge_output="$($GIT_BIN -C "$wt_path" merge "$source_ref" --no-edit 2>&1)"
    merge_rc=$?
    if [[ "$merge_rc" -eq 0 ]]; then
        return 0
    fi

    unresolved="$($GIT_BIN -C "$wt_path" diff --name-only --diff-filter=U 2>/dev/null || true)"
    if [[ -n "$unresolved" ]] || "$GIT_BIN" -C "$wt_path" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
        if resolve_conflict_with_claude "$wt_path" "$dir_name" "$branch" "$source_ref"; then
            return 0
        fi
        "$GIT_BIN" -C "$wt_path" merge --abort >/dev/null 2>&1 || true
        ((conflict_failed++))
        record_failure "$branch" "conflict_resolution_failed" \
            "automated conflict resolution failed or timed out for $source_ref" true
        add_summary "ESCALATE (conflict unresolved): $dir_name [$branch]"
        return 1
    fi

    classify_failure "$merge_output"
    if [[ "$FAILURE_CLASS" == "unknown" ]]; then
        FAILURE_CLASS="environment_fault"
        FAILURE_IMMEDIATE=true
    fi
    record_failure "$branch" "$FAILURE_CLASS" "merge from $source_ref failed before conflict resolution" "$FAILURE_IMMEDIATE"
    add_summary "FAIL ($FAILURE_CLASS merging $source_ref): $dir_name [$branch]"
    return 1
}

prepare_push() {
    local branch="$1"
    if ! touch "$BYPASS_FILE" 2>>"$LOG_FILE"; then
        record_failure "$branch" "environment_fault" "cannot create compile-bypass marker" true
        return 1
    fi
    return 0
}

record_push_failure() {
    local branch="$1"
    local dir_name="$2"
    local output="$3"
    local context="$4"
    classify_failure "$output"
    if [[ "$FAILURE_CLASS" == "non_fast_forward" ]]; then
        FAILURE_CLASS="non_fast_forward_race"
    fi
    record_failure "$branch" "$FAILURE_CLASS" "$context" "$FAILURE_IMMEDIATE"
    add_summary "FAIL ($FAILURE_CLASS): $dir_name [$branch]"
}

recover_non_fast_forward() {
    local wt_path="$1"
    local dir_name="$2"
    local branch="$3"
    local previous_remote fetch_output fetch_rc current_remote retry_output retry_rc

    previous_remote="$($GIT_BIN -C "$wt_path" rev-parse "origin/$branch" 2>/dev/null || true)"
    log "  Push rejected non-fast-forward for $dir_name; fetching and merging origin/$branch..."
    fetch_output="$($GIT_BIN -C "$wt_path" fetch origin "$branch" 2>&1)"
    fetch_rc=$?
    if [[ "$fetch_rc" -ne 0 ]]; then
        classify_failure "$fetch_output"
        record_failure "$branch" "$FAILURE_CLASS" "fetch for non-fast-forward recovery failed" "$FAILURE_IMMEDIATE"
        add_summary "FAIL ($FAILURE_CLASS during NFF recovery): $dir_name [$branch]"
        return 1
    fi

    current_remote="$($GIT_BIN -C "$wt_path" rev-parse "origin/$branch" 2>/dev/null || true)"
    if [[ -z "$current_remote" ]]; then
        record_failure "$branch" "remote_gone" "origin branch disappeared during non-fast-forward recovery" true
        add_summary "ESCALATE (remote gone): $dir_name [$branch]"
        return 1
    fi
    if [[ -n "$previous_remote" && "$previous_remote" != "$current_remote" ]] && \
       ! "$GIT_BIN" -C "$wt_path" merge-base --is-ancestor "$previous_remote" "$current_remote" >/dev/null 2>&1; then
        record_failure "$branch" "upstream_rewritten" "remote history was force-pushed or rewritten" true
        add_summary "ESCALATE (upstream rewritten): $dir_name [$branch]"
        return 1
    fi
    if ! validate_remote_ownership "$wt_path" "$branch"; then
        return 1
    fi
    if ! merge_source "$wt_path" "$dir_name" "$branch" "origin/$branch"; then
        return 1
    fi
    if ! prepare_push "$branch"; then
        add_summary "FAIL (compile-bypass marker): $dir_name [$branch]"
        return 1
    fi

    retry_output="$($GIT_BIN -C "$wt_path" push 2>&1)"
    retry_rc=$?
    if [[ "$retry_rc" -eq 0 ]]; then
        log "  Non-fast-forward repaired and pushed for $dir_name"
        reset_failure "$branch"
        ((pushed++))
        add_summary "NFF-RECOVERED+PUSHED: $dir_name [$branch]"
        return 0
    fi
    record_push_failure "$branch" "$dir_name" "$retry_output" \
        "push still failed after non-fast-forward recovery"
    return 1
}

push_branch() {
    local wt_path="$1"
    local dir_name="$2"
    local branch="$3"
    local push_output push_rc

    if ! prepare_push "$branch"; then
        add_summary "FAIL (compile-bypass marker): $dir_name [$branch]"
        return 1
    fi
    push_output="$($GIT_BIN -C "$wt_path" push 2>&1)"
    push_rc=$?
    if [[ "$push_rc" -eq 0 ]]; then
        reset_failure "$branch"
        ((pushed++))
        add_summary "PUSHED: $dir_name [$branch]"
        return 0
    fi

    classify_failure "$push_output"
    if [[ "$FAILURE_CLASS" == "non_fast_forward" ]]; then
        recover_non_fast_forward "$wt_path" "$dir_name" "$branch"
        return $?
    fi
    record_failure "$branch" "$FAILURE_CLASS" "push failed" "$FAILURE_IMMEDIATE"
    add_summary "FAIL ($FAILURE_CLASS push): $dir_name [$branch]"
    return 1
}

send_escalations() {
    local text index notify_rc
    text=":warning: *Daily Merge Stable escalation*"
    for ((index=0; index<${#escalation_branches[@]}; index++)); do
        text+=$'\n'
        text+="• \`${escalation_branches[$index]}\` — *${escalation_classes[$index]}* (streak ${escalation_counts[$index]}): ${escalation_details[$index]}"
    done
    text+=$'\n'
    text+="Log: \`$LOG_FILE\`"

    log "Sending ${#escalation_branches[@]} escalation(s) to Slack channel $SLACK_CHANNEL..."
    if [[ -n "$NOTIFIER_BIN" ]]; then
        DAILY_MERGE_SLACK_TEXT="$text" DAILY_MERGE_SLACK_CHANNEL="$SLACK_CHANNEL" \
            "$NOTIFIER_BIN" >>"$LOG_FILE" 2>&1
        notify_rc=$?
    else
        DAILY_MERGE_SLACK_TEXT="$text" DAILY_MERGE_SLACK_CHANNEL="$SLACK_CHANNEL" \
            PYTHONPATH="$WAKELITE_REPO${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" -c 'import os; from wakelite.notifier import Notifier; ts = Notifier().notify_slack(os.environ["DAILY_MERGE_SLACK_TEXT"], channel=os.environ["DAILY_MERGE_SLACK_CHANNEL"]); raise SystemExit(0 if ts else 1)' \
            >>"$LOG_FILE" 2>&1
        notify_rc=$?
    fi
    if [[ "$notify_rc" -ne 0 ]]; then
        log "ERROR: Slack escalation delivery failed (exit $notify_rc); leaving alerts pending"
        return 1
    fi

    for ((index=0; index<${#escalation_state_files[@]}; index++)); do
        if ! mark_state_alerted "${escalation_state_files[$index]}"; then
            log "ERROR: Slack delivered but could not mark alert state for ${escalation_branches[$index]}"
            return 1
        fi
    done
    log "Slack escalation delivered."
    return 0
}

finish_run() {
    local index
    log ""
    log "=========================================="
    log "  Daily Merge Summary — $(date '+%Y-%m-%d %H:%M')"
    log "=========================================="
    log "  Merged:              $merged"
    log "  Pushed:              $pushed"
    log "  Conflicts resolved:  $conflict_resolved"
    log "  Conflicts unresolved:$conflict_failed"
    log "  Already up-to-date:  $already_uptodate"
    log "  Failures this run:   $failure_count"
    log "  Skipped:             $skipped"
    log "=========================================="
    for ((index=0; index<${#summary_lines[@]}; index++)); do
        log "    ${summary_lines[$index]}"
    done

    if [[ ${#escalation_branches[@]} -gt 0 ]]; then
        if ! send_escalations; then
            return 1
        fi
        return 1
    fi
    if [[ "$has_active_escalation" == "true" ]]; then
        log "An already-escalated failure remains unresolved; no duplicate Slack post."
        return 1
    fi
    if [[ "$had_failures" == "true" ]]; then
        log "Routine failures remain below escalation threshold; returning EX_TEMPFAIL."
        return 75
    fi
    log "Run completed without escalations; Slack remained silent."
    return 0
}

case "$STREAK_THRESHOLD" in
    ''|*[!0-9]*|0)
        printf 'daily-merge-stable: DAILY_MERGE_STREAK_THRESHOLD must be a positive integer\n' >&2
        exit 1
        ;;
esac

if [[ -z "$PRODUCTS_REPO" || -z "$WORKTREE_DIR" || -z "$MERGE_REF" || \
      -z "$SLACK_CHANNEL" || -z "$BYPASS_FILE" ]]; then
    printf '%s\n' \
        'daily-merge-stable: set DAILY_MERGE_PRODUCTS_REPO, DAILY_MERGE_WORKTREE_DIR, DAILY_MERGE_REF, DAILY_MERGE_SLACK_CHANNEL, and DAILY_MERGE_BYPASS_FILE' >&2
    exit 1
fi
if [[ -z "$NOTIFIER_BIN" && -z "$WAKELITE_REPO" ]]; then
    printf '%s\n' \
        'daily-merge-stable: set DAILY_MERGE_WAKELITE_REPO or DAILY_MERGE_NOTIFIER_BIN' >&2
    exit 1
fi

if ! mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"; then
    printf 'daily-merge-stable: cannot create state or log directory\n' >&2
    exit 1
fi
if [[ ! -d "$PRODUCTS_REPO" ]]; then
    log "ERROR: configured repository is missing: $PRODUCTS_REPO"
    record_failure "__environment__" "environment_fault" "configured repository is missing" true
    finish_run
    exit $?
fi

log "=== Daily Merge Stable starting ==="
REMOTE_SNAPSHOT="$(mktemp "$STATE_DIR/remote-refs.XXXXXX")"
if ! "$GIT_BIN" -C "$PRODUCTS_REPO" for-each-ref \
    --format='%(refname:short) %(objectname)' refs/remotes/origin >"$REMOTE_SNAPSHOT"; then
    log "ERROR: could not snapshot origin refs before fetch"
    record_failure "__fetch__" "environment_fault" "could not snapshot remote refs" true
    finish_run
    exit $?
fi

log "Fetching latest origin refs and moving tags..."
fetch_output="$($GIT_BIN -C "$PRODUCTS_REPO" fetch --prune 2>&1)"
fetch_rc=$?
tags_output="$($GIT_BIN -C "$PRODUCTS_REPO" fetch --tags --force 2>&1)"
tags_rc=$?

if [[ "$fetch_rc" -ne 0 ]] && ! grep -Eqi \
    'cannot lock ref.+exists; cannot create|would clobber existing tag' <<<"$fetch_output"; then
    classify_failure "$fetch_output"
    record_failure "__fetch__" "$FAILURE_CLASS" "origin fetch failed" "$FAILURE_IMMEDIATE"
    add_summary "FAIL ($FAILURE_CLASS): origin fetch"
    finish_run
    exit $?
fi
if [[ "$tags_rc" -ne 0 ]]; then
    classify_failure "$tags_output"
    if [[ "$FAILURE_CLASS" == "unknown" ]]; then
        FAILURE_CLASS="environment_fault"
        FAILURE_IMMEDIATE=true
    fi
    record_failure "__fetch__" "$FAILURE_CLASS" "$MERGE_REF tag fetch failed" "$FAILURE_IMMEDIATE"
    add_summary "FAIL ($FAILURE_CLASS): tag fetch"
    finish_run
    exit $?
fi
if ! "$GIT_BIN" -C "$PRODUCTS_REPO" rev-parse "$MERGE_REF" >/dev/null 2>&1; then
    record_failure "__fetch__" "environment_fault" "$MERGE_REF is unreachable after fetch" true
    add_summary "FAIL (environment_fault): $MERGE_REF unavailable"
    finish_run
    exit $?
fi
reset_failure "__fetch__"
log "Fetch complete."

STABLE_WT="${WORKTREE_DIR}/stable"
if [[ -d "$STABLE_WT" ]]; then
    stable_sha="$($GIT_BIN -C "$PRODUCTS_REPO" rev-parse "$MERGE_REF" 2>/dev/null || true)"
    current_sha="$($GIT_BIN -C "$STABLE_WT" rev-parse HEAD 2>/dev/null || true)"
    if [[ -z "$stable_sha" || -z "$current_sha" ]]; then
        record_failure "__stable_worktree__" "environment_fault" "stable worktree cannot resolve HEAD" true
        add_summary "FAIL (environment_fault): stable worktree"
    elif [[ "$stable_sha" != "$current_sha" ]]; then
        if ! stable_update_output="$($GIT_BIN -C "$STABLE_WT" checkout "$MERGE_REF" 2>&1)"; then
            log "Stable worktree update failed: $stable_update_output"
            record_failure "__stable_worktree__" "environment_fault" "stable worktree update failed" true
            add_summary "FAIL (environment_fault): stable worktree update"
        else
            reset_failure "__stable_worktree__"
        fi
    else
        reset_failure "__stable_worktree__"
    fi
fi

if ! worktree_output="$($GIT_BIN -C "$PRODUCTS_REPO" worktree list --porcelain 2>&1)"; then
    record_failure "__environment__" "environment_fault" "git worktree list failed" true
    add_summary "FAIL (environment_fault): worktree inventory"
    finish_run
    exit $?
fi

while IFS= read -r wt_path; do
    [[ -z "$wt_path" ]] && continue
    if [[ "$wt_path" == "$PRODUCTS_REPO" ]]; then
        add_summary "SKIP (main repo): $(basename "$wt_path")"
        ((skipped++))
        continue
    fi
    if [[ "$wt_path" != "$WORKTREE_DIR"/* ]]; then
        add_summary "SKIP (outside managed worktrees): $(basename "$wt_path")"
        ((skipped++))
        continue
    fi

    dir_name="$(basename "$wt_path")"
    if [[ "$dir_name" == "stable" ]]; then
        add_summary "SKIP (stable): $dir_name"
        ((skipped++))
        continue
    fi
    if [[ ! -d "$wt_path" ]]; then
        record_failure "$dir_name" "environment_fault" "registered worktree directory is missing" true
        add_summary "ESCALATE (worktree missing): $dir_name"
        continue
    fi

    branch="$($GIT_BIN -C "$wt_path" symbolic-ref --short HEAD 2>/dev/null || true)"
    if [[ -z "$branch" ]]; then
        record_failure "$dir_name" "environment_fault" "managed worktree has detached HEAD" true
        add_summary "ESCALATE (detached HEAD): $dir_name"
        continue
    fi
    if ! "$GIT_BIN" check-ref-format --branch "$branch" >/dev/null 2>&1; then
        record_failure "$dir_name" "environment_fault" "managed worktree has invalid branch name" true
        add_summary "ESCALATE (invalid branch): $dir_name"
        continue
    fi
    if is_excluded_branch "$branch"; then
        reset_failure "$branch"
        add_summary "SKIP (excluded): $dir_name [$branch]"
        ((skipped++))
        continue
    fi

    upstream_status="$($GIT_BIN -C "$wt_path" for-each-ref --format='%(upstream:track)' "refs/heads/$branch" 2>/dev/null || true)"
    if [[ "$upstream_status" == "[gone]" ]]; then
        record_failure "$branch" "remote_gone" "tracked remote branch was deleted" true
        add_summary "ESCALATE (remote gone): $dir_name [$branch]"
        continue
    fi
    if remote_was_rewritten "$wt_path" "$branch"; then
        record_failure "$branch" "upstream_rewritten" "remote history changed non-fast-forward during fetch" true
        add_summary "ESCALATE (upstream rewritten): $dir_name [$branch]"
        continue
    fi

    branch_authors="$($GIT_BIN -C "$wt_path" log "$MERGE_REF..HEAD" --no-merges --format='%an' 2>/dev/null || true)"
    if [[ -z "$branch_authors" ]]; then
        reset_failure "$branch"
        add_summary "SKIP (no owned feature commits): $dir_name [$branch]"
        ((skipped++))
        continue
    fi
    FOREIGN_AUTHOR=""
    if ! all_authors_are_trusted "$branch_authors"; then
        record_failure "$branch" "ownership_change" \
            "non-merge feature commit is authored by ${FOREIGN_AUTHOR:-another owner}" true
        add_summary "ESCALATE (ownership changed): $dir_name [$branch]"
        continue
    fi
    if "$GIT_BIN" -C "$wt_path" rev-parse "origin/$branch" >/dev/null 2>&1; then
        if ! validate_remote_ownership "$wt_path" "$branch"; then
            continue
        fi
        divergence="$($GIT_BIN -C "$wt_path" rev-list --left-right --count "HEAD...origin/$branch" 2>/dev/null || true)"
        ahead="$(printf '%s\n' "$divergence" | awk '{print $1}')"
        behind="$(printf '%s\n' "$divergence" | awk '{print $2}')"
        case "$ahead:$behind" in
            *[!0-9:]*|:|*:)
                record_failure "$branch" "environment_fault" "cannot calculate local/remote divergence" true
                add_summary "ESCALATE (bad divergence state): $dir_name [$branch]"
                continue
                ;;
        esac
        if [[ "$behind" -gt 0 ]]; then
            log "Reconciling origin/$branch into $dir_name (ahead=$ahead behind=$behind)..."
            if ! merge_source "$wt_path" "$dir_name" "$branch" "origin/$branch"; then
                continue
            fi
            ((merged++))
        fi
    fi

    merge_base="$($GIT_BIN -C "$wt_path" merge-base HEAD "$MERGE_REF" 2>/dev/null || true)"
    stable_head="$($GIT_BIN -C "$wt_path" rev-parse "$MERGE_REF" 2>/dev/null || true)"
    if [[ -z "$merge_base" || -z "$stable_head" ]]; then
        record_failure "$branch" "environment_fault" "cannot calculate merge base against $MERGE_REF" true
        add_summary "ESCALATE (merge-base unavailable): $dir_name [$branch]"
        continue
    fi
    if [[ "$merge_base" != "$stable_head" ]]; then
        log "Merging $MERGE_REF into $dir_name [$branch]..."
        if ! merge_source "$wt_path" "$dir_name" "$branch" "$MERGE_REF"; then
            continue
        fi
        ((merged++))
    fi

    if "$GIT_BIN" -C "$wt_path" rev-parse "origin/$branch" >/dev/null 2>&1; then
        ahead_after="$($GIT_BIN -C "$wt_path" rev-list --count "origin/$branch..HEAD" 2>/dev/null || true)"
    else
        ahead_after=1
    fi
    case "$ahead_after" in
        ''|*[!0-9]*)
            record_failure "$branch" "environment_fault" "cannot determine whether branch needs a push" true
            add_summary "ESCALATE (push state unavailable): $dir_name [$branch]"
            continue
            ;;
    esac
    if [[ "$ahead_after" -gt 0 ]]; then
        push_branch "$wt_path" "$dir_name" "$branch" || true
    else
        reset_failure "$branch"
        add_summary "UP-TO-DATE: $dir_name [$branch]"
        ((already_uptodate++))
    fi
done < <(printf '%s\n' "$worktree_output" | sed -n 's/^worktree //p')

finish_run
exit $?
