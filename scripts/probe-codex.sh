#!/usr/bin/env bash
# Check whether Codex will actually run this repository's project-local hook, and
# say precisely which condition is missing if it will not.
#
# Two independent conditions must both hold before Codex loads a project-level
# `.codex/hooks.json`. Both were established experimentally against codex-cli
# 0.154.0; see docs/guarantees.md.
#
#   1. The repository is trusted, as a *persisted* entry in $CODEX_HOME/config.toml:
#        [projects."<absolute repo path>"]
#        trust_level = "trusted"
#      Passing the same key with `-c` does NOT work. This is the condition that made
#      an earlier investigation conclude, wrongly, that project hooks never load.
#
#   2. The hook's content is trusted: either the correct `trusted_hash`, which the
#      /hooks TUI writes when you approve the hook, or --dangerously-bypass-hook-trust
#      for automation that has already vetted the script.
#
# `enabled` is not a third condition — a hook with no `hooks.state` entry is enabled.
# Editing the command string changes its hash and revokes condition 2.
#
# This script runs one disposable Codex turn against a temporary CODEX_HOME, so the
# real configuration is never modified. It needs Codex API quota.
set -euo pipefail

TARGET="$PWD"
MODEL=""
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --target=*) TARGET="${1#--target=}"; shift ;;
        --model) MODEL="$2"; shift 2 ;;
        --model=*) MODEL="${1#--model=}"; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "probe-codex.sh: unrecognized argument $1" >&2; exit 2 ;;
    esac
done

command -v codex >/dev/null 2>&1 || {
    echo "probe-codex.sh: the codex CLI is not on PATH" >&2; exit 2; }
TARGET=$(cd -- "$TARGET" && pwd -P)
REAL_HOME="${CODEX_HOME:-$HOME/.codex}"

echo "=== Condition check against $REAL_HOME/config.toml (read-only) ==="
if grep -qF "[projects.\"$TARGET\"]" "$REAL_HOME/config.toml" 2>/dev/null; then
    echo "  1. repository trust ......... present"
else
    echo "  1. repository trust ......... MISSING"
    echo "     Codex will not load $TARGET/.codex/hooks.json at all."
    echo "     Open the repository in the Codex TUI once and accept the trust prompt,"
    echo "     or add to $REAL_HOME/config.toml:"
    echo "       [projects.\"$TARGET\"]"
    echo "       trust_level = \"trusted\""
fi
if grep -qF "$TARGET/.codex/hooks.json:user_prompt_submit" "$REAL_HOME/config.toml" 2>/dev/null; then
    echo "  2. hook content trust ....... present"
else
    echo "  2. hook content trust ....... MISSING"
    echo "     Run /hooks in the Codex TUI and approve the UserPromptSubmit hook."
    echo "     The entry will be keyed as:"
    echo "       [hooks.state.\"$TARGET/.codex/hooks.json:user_prompt_submit:0:0\"]"
fi

echo
echo "=== Live check in an isolated CODEX_HOME ==="
WORK=$(mktemp -d "${TMPDIR:-/tmp}/codexprobe.XXXXXX")
trap '[ "$KEEP" -eq 1 ] || rm -rf "$WORK"' EXIT
HOME_DIR="$WORK/home"
mkdir -p "$HOME_DIR"
# Auth lives in CODEX_HOME, so it has to come along or the turn cannot start.
[ -f "$REAL_HOME/auth.json" ] && cp "$REAL_HOME/auth.json" "$HOME_DIR/" || true
cat > "$HOME_DIR/config.toml" <<EOF
[projects."$TARGET"]
trust_level = "trusted"
EOF

MARKER="$WORK/fired"
set +e
( cd "$TARGET" && env CODEX_HOME="$HOME_DIR" SPECKIT_PREHOOK_PROBE="$MARKER" \
    RUST_LOG=codex_hooks=debug \
    codex exec --skip-git-repo-check --dangerously-bypass-hook-trust \
        ${MODEL:+-m "$MODEL"} 'Reply with the single word ok.' \
    </dev/null > "$WORK/out.log" 2> "$WORK/err.log" )
status=$?
set -e

if grep -qi "^hook: UserPromptSubmit" "$WORK/err.log"; then
    echo "  Codex invoked the UserPromptSubmit hook."
    if grep -qi "^hook: UserPromptSubmit Completed" "$WORK/err.log"; then
        echo "  The hook completed successfully."
    else
        echo "  The hook FAILED to run. Check that the command in"
        echo "  $TARGET/.codex/hooks.json is executable and that its path needs no"
        echo "  shell quoting — the command string is evaluated by a shell."
    fi
else
    echo "  Codex did NOT invoke the hook."
    echo "  With repository trust supplied and hook trust bypassed above, the most"
    echo "  likely causes are a missing or malformed $TARGET/.codex/hooks.json, or a"
    echo "  Codex version whose project layer behaves differently from 0.154.0."
fi
if grep -qi "usage limit" "$WORK/err.log"; then
    echo "  Note: the turn hit an API usage limit. Hook delivery is still reported"
    echo "  accurately above, because hooks run before the model request."
fi
echo "  codex exit status: $status"
[ "$KEEP" -eq 1 ] && echo "  Logs kept at $WORK" || true
