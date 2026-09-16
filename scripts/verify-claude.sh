#!/usr/bin/env bash
# Live end-to-end check of the Claude Code path.
#
# Assertions are made against files written by a spy wrapper rather than against the
# model's prose. That keeps the check deterministic and cheap: the wrapper records
# the raw hook payload and our exact stdout, so "which event fired, how often, and
# what did we emit" is answered without depending on what the model chose to say.
# The model's reply is only used as a secondary liveness signal.
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
KEEP=0
MODEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --model) MODEL="$2"; shift 2 ;;
        --model=*) MODEL="${1#--model=}"; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "verify-claude.sh: unrecognized argument $1" >&2; exit 2 ;;
    esac
done

command -v claude >/dev/null 2>&1 || {
    echo "verify-claude.sh: the claude CLI is not on PATH" >&2; exit 2; }

FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/speckit verify XXXXXX")
trap '[ "$KEEP" -eq 1 ] || rm -rf "$FIXTURE"' EXIT
REPO="$FIXTURE/repo"
mkdir -p "$REPO"/{.artifacts,.specify/extensions/probe,hook} \
         "$REPO"/.claude/skills/{speckit-plan,speckit-probe-check}
git init -q "$REPO"

cp -R "$SOURCE_DIR/template/.speckit-hooks/." "$REPO/.speckit-hooks/" 2>/dev/null \
    || { mkdir -p "$REPO/.speckit-hooks"; cp -R "$SOURCE_DIR/template/.speckit-hooks/." "$REPO/.speckit-hooks/"; }
rm -rf "$REPO/.speckit-hooks/speckit_prehook/__pycache__"

cat > "$REPO/hook/spy" <<'SPY'
#!/bin/sh
# Record the payload, run the real hook, record its stdout, preserve its status.
set -eu
here=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
payload=$(cat)
printf '%s\n' "$payload" >> "$here/.artifacts/payloads.jsonl"
out=$(printf '%s' "$payload" | "$here/.speckit-hooks/speckit-hook" "$@" 2>"$here/.artifacts/stderr.log") \
    && status=0 || status=$?
printf '%s' "$out" >> "$here/.artifacts/stdout.jsonl"
printf '\n' >> "$here/.artifacts/stdout.jsonl"
printf '%s' "$out"
exit "$status"
SPY
chmod +x "$REPO/hook/spy"

cat > "$REPO/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "UserPromptExpansion": [
      { "matcher": "speckit[-.]",
        "hooks": [{ "type": "command",
                    "command": "\"$CLAUDE_PROJECT_DIR/hook/spy\" --agent=claude",
                    "timeout": 20 }] }
    ],
    "PreToolUse": [
      { "matcher": "Skill",
        "hooks": [{ "type": "command",
                    "command": "\"$CLAUDE_PROJECT_DIR/hook/spy\" --agent=claude",
                    "timeout": 10 }] }
    ]
  }
}
JSON

cat > "$REPO/.specify/extensions.yml" <<'YAML'
installed:
- probe
settings:
  auto_execute_hooks: true
hooks:
  before_plan:
  - extension: probe
    command: speckit.probe.check
    enabled: true
    optional: false
    priority: 10
    prompt: Execute speckit.probe.check?
    description: Live verification pre-hook
    condition: null
YAML

cat > "$REPO/.specify/extensions/probe/extension.yml" <<'YAML'
schema_version: "1.0"
extension:
  id: probe
  name: Probe
provides:
  commands:
  - name: speckit.probe.check
    file: commands/check.md
YAML

cat > "$REPO/.claude/skills/speckit-probe-check/SKILL.md" <<'MD'
---
name: speckit-probe-check
description: Live verification pre-hook.
metadata:
  author: github-spec-kit
  source: probe:commands/check.md
---

Output the exact token PREHOOK_7f3a9c on its own line. Use no tools and change no
files. Then continue with the original workflow.
MD

cat > "$REPO/.claude/skills/speckit-plan/SKILL.md" <<'MD'
---
name: speckit-plan
description: Stand-in for the Spec Kit plan stage.
---

Output the exact token MAIN_41b8d2 on its own line. Use no tools and change no
files. Say nothing else.
MD

run_case() {
    label="$1"; prompt="$2"
    rm -f "$REPO/.artifacts/"*.jsonl "$REPO/.artifacts/stderr.log"
    set +e
    ( cd "$REPO" && claude -p "$prompt" \
        --setting-sources project --strict-mcp-config \
        --tools Skill --allowedTools Skill \
        --output-format json --max-turns 2 \
        ${MODEL:+--model "$MODEL"} \
        > "$REPO/.artifacts/result.json" 2>"$REPO/.artifacts/cli.log" )
    status=$?
    set -e
    printf '\n=== %s (claude exit %d) ===\n' "$label" "$status"
    python3 - "$REPO" "$label" <<'PYEOF'
import json, os, sys
repo, label = sys.argv[1], sys.argv[2]
art = os.path.join(repo, ".artifacts")

def lines(name):
    path = os.path.join(art, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]

payloads = [json.loads(line) for line in lines("payloads.jsonl")]
events = [item.get("hook_event_name") for item in payloads]
print("hook invocations:", events or "(none)")
for item in payloads:
    print("  fields:", ", ".join(sorted(item)))
    print("  prompt_id present:", "prompt_id" in item)

emitted = [json.loads(line) for line in lines("stdout.jsonl")]
injected = "".join(d.get("hookSpecificOutput", {}).get("additionalContext", "")
                   for d in emitted)
print("injected context bytes:", len(injected.encode("utf-8")))
print("PREHOOK token present in injection:", "PREHOOK_7f3a9c" in injected)

result_path = os.path.join(art, "result.json")
if os.path.exists(result_path):
    try:
        with open(result_path, encoding="utf-8") as handle:
            reply = json.load(handle).get("result", "")
    except ValueError:
        reply = ""
    pre, main = reply.find("PREHOOK_7f3a9c"), reply.find("MAIN_41b8d2")
    print("reply contains pre-hook token:", pre >= 0)
    print("reply contains stage token:", main >= 0)
    print("pre-hook precedes stage:", pre >= 0 and main >= 0 and pre < main)
PYEOF
}

run_case "typed slash command -> UserPromptExpansion" "/speckit-plan"
run_case "model skill invocation -> PreToolUse" \
    "Use the speckit-plan skill now. This is an integration test."

cat <<NOTE

What to read above
  * "hook invocations" should list exactly one event per case, confirming that the
    two entry points do not both fire and cannot double-inject.
  * "PREHOOK token present in injection" is the deterministic result: it proves the
    runner read the installed SKILL.md and handed its real body to the session.
  * The "reply contains" lines are the model-dependent part. A false there means the
    model did not follow injected instructions, which is exactly the limitation
    documented in docs/guarantees.md — not a runner failure.
NOTE
[ "$KEEP" -eq 1 ] && echo "Fixture kept at $FIXTURE" || true
