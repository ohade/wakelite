#!/usr/bin/env bash
# Install the wakelite-scheduling Claude Code skill.
# Renders template/ into the skills directory, replacing the machine-specific
# placeholders ({{WAKELITE_REPO}}, {{WAKELITE_HOME}}, {{INSTALL_DATE}}).
# Bash 3.2 compatible (stock macOS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/template"
SKILL_NAME="wakelite-scheduling"

REPO=""
HOME_DIR="${WAKELITE_HOME:-$HOME/.wakelite}"
DEST="$HOME/.claude/skills/$SKILL_NAME"
DRY_RUN=0

usage() {
    cat <<EOF
Usage: ./install.sh [options]

Installs the $SKILL_NAME skill for Claude Code.

Options:
  --repo PATH        WakeLite checkout (default: the checkout this script lives in)
  --home PATH        WakeLite state directory (default: \$WAKELITE_HOME or ~/.wakelite)
  --dest PATH        Skill install directory (default: ~/.claude/skills/$SKILL_NAME)
  --dry-run          Render into a temp directory and show the result; change nothing
  -h, --help         Show this help

An existing skill at --dest is moved to <dest>.bak-<timestamp>, never deleted.
EOF
}

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
info() { printf '%s\n' "$1"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) [ $# -ge 2 ] || die "--repo needs a path"; REPO="$2"; shift 2 ;;
        --home) [ $# -ge 2 ] || die "--home needs a path"; HOME_DIR="$2"; shift 2 ;;
        --dest) [ $# -ge 2 ] || die "--dest needs a path"; DEST="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

expand_path() {
    # Expand a leading ~ and make the path absolute without requiring it to exist.
    local p="$1"
    case "$p" in
        "~") p="$HOME" ;;
        "~/"*) p="$HOME/${p#\~/}" ;;
    esac
    case "$p" in
        /*) ;;
        *) p="$PWD/$p" ;;
    esac
    printf '%s' "${p%/}"
}

is_wakelite_repo() {
    [ -x "$1/bin/wakelitectl" ] && [ -f "$1/wakelite/cli.py" ]
}

[ -f "$TEMPLATE_DIR/SKILL.md" ] || die "template not found at $TEMPLATE_DIR"

# --- Resolve the WakeLite repo -------------------------------------------------
# This script ships in <repo>/skill/, so the enclosing checkout is the default.
[ -n "$REPO" ] || REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO="$(expand_path "$REPO")"
HOME_DIR="$(expand_path "$HOME_DIR")"
DEST="$(expand_path "$DEST")"

# The rendered skill contains copy-paste shell lines such as WL=<repo>/bin/wakelitectl,
# so an injected path must be safe unquoted.
for pair in "--repo:$REPO" "--home:$HOME_DIR"; do
    case "${pair#*:}" in
        *[!A-Za-z0-9._/@+-]*) die "${pair%%:*} path has spaces or shell characters: ${pair#*:}" ;;
    esac
done

is_wakelite_repo "$REPO" || die "$REPO is not a WakeLite checkout (no bin/wakelitectl); fix --repo"

# --- Render ---------------------------------------------------------------------
INSTALL_DATE="$(date +%Y-%m-%d)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/wakelite-skill.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

replace_all() {
    # Literal replace of $2 with $3 in $1. Avoids ${var//pat/rep}, whose quoting
    # differs between bash 3.2 and 5.2 (patsub_replacement treats '&' specially).
    local rest="$1" pat="$2" rep="$3" out=""
    while [ "${rest#*"$pat"}" != "$rest" ]; do
        out="$out${rest%%"$pat"*}$rep"
        rest="${rest#*"$pat"}"
    done
    printf '%s' "$out$rest"
}

render_file() {
    local src="$1" dst="$2" content
    content="$(cat "$src"; printf x)"
    content="${content%x}"
    content="$(replace_all "$content" '{{WAKELITE_REPO}}' "$REPO"; printf x)"; content="${content%x}"
    content="$(replace_all "$content" '{{WAKELITE_HOME}}' "$HOME_DIR"; printf x)"; content="${content%x}"
    content="$(replace_all "$content" '{{INSTALL_DATE}}' "$INSTALL_DATE"; printf x)"; content="${content%x}"
    mkdir -p "$(dirname "$dst")"
    printf '%s' "$content" > "$dst"
}

count=0
while IFS= read -r -d '' src; do
    rel="${src#"$TEMPLATE_DIR"/}"
    render_file "$src" "$STAGE/$rel"
    count=$((count + 1))
done < <(find "$TEMPLATE_DIR" -type f -name '*.md' -print0)

[ "$count" -gt 0 ] || die "no template files rendered"

if leftover="$(grep -rn '{{[A-Z_]*}}' "$STAGE")"; then
    printf '%s\n' "$leftover" >&2
    die "unreplaced placeholders remain; nothing installed"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] rendered $count files with:"
    info "  WAKELITE_REPO = $REPO"
    info "  WAKELITE_HOME = $HOME_DIR"
    info "[dry-run] would install to $DEST"
    (cd "$STAGE" && find . -type f | sort | sed 's|^\./|  |')
    info "--- SKILL.md (first 20 lines) ---"
    sed -n 1,20p "$STAGE/SKILL.md"
    exit 0
fi

# --- Install --------------------------------------------------------------------
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    BACKUP="$DEST.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$DEST" "$BACKUP"
    info "Moved existing skill to $BACKUP"
fi

mkdir -p "$(dirname "$DEST")"
cp -R "$STAGE" "$DEST"
chmod 755 "$DEST" "$DEST/references" 2>/dev/null || true
find "$DEST" -type f -exec chmod 644 {} +

info "Installed $SKILL_NAME ($count files) to $DEST"
info "  WAKELITE_REPO = $REPO"
info "  WAKELITE_HOME = $HOME_DIR"

# --- Post-install check (informational) ------------------------------------------
if "$REPO/bin/wakelitectl" health >/dev/null 2>&1; then
    info "WakeLite runner: reachable"
else
    info "WakeLite runner: not reachable. Install it with:"
    info "  $REPO/bin/wakelitectl launchd install --scope user --load"
fi
info "Start a new Claude Code session to load the skill."
