#!/usr/bin/env bash
# Install the deterministic Spec Kit pre-hook runner into a repository.
#
# Copies `.speckit-hooks/` and merges the hook registrations into the target's
# `.claude/settings.json` and `.codex/hooks.json`, preserving any hooks already
# configured there.
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEMPLATE="$SOURCE_DIR/template"

usage() {
    cat <<'USAGE'
Usage: scripts/install.sh [--target DIR] [--agent claude|codex|both] [--force]

  --target DIR   Repository to install into (default: the current directory).
  --agent        Which CLI configuration to merge (default: both).
  --force        Overwrite an existing .speckit-hooks directory.

After installing, restart the CLI session: Claude Code snapshots hooks at session
start, and Codex requires the new hook to be approved with its /hooks command.
USAGE
}

TARGET="$PWD"
AGENT="both"
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --target=*) TARGET="${1#--target=}"; shift ;;
        --agent) AGENT="$2"; shift 2 ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unrecognized argument $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$AGENT" in claude|codex|both) ;; *)
    echo "install.sh: --agent must be claude, codex or both" >&2; exit 2 ;;
esac
[ -d "$TARGET" ] || { echo "install.sh: no such directory: $TARGET" >&2; exit 2; }
TARGET=$(cd -- "$TARGET" && pwd -P)

if [ -e "$TARGET/.speckit-hooks" ] && [ "$FORCE" -eq 0 ]; then
    echo "install.sh: $TARGET/.speckit-hooks already exists; pass --force to replace it" >&2
    exit 2
fi
rm -rf "$TARGET/.speckit-hooks"
mkdir -p "$TARGET/.speckit-hooks"
# `cp -R src/.` copies contents rather than the directory itself, which behaves the
# same way on both GNU and BSD cp.
cp -R "$TEMPLATE/.speckit-hooks/." "$TARGET/.speckit-hooks/"
rm -rf "$TARGET/.speckit-hooks/speckit_prehook/__pycache__"
chmod +x "$TARGET/.speckit-hooks/speckit-hook"

merge() {
    python3 - "$1" "$2" <<'PYEOF'
import json, os, sys

template_path, target_path = sys.argv[1], sys.argv[2]
with open(template_path, encoding="utf-8") as handle:
    template = json.load(handle)

target = {}
if os.path.exists(target_path):
    with open(target_path, encoding="utf-8") as handle:
        target = json.load(handle)

hooks = target.setdefault("hooks", {})
added = 0
for event, groups in template["hooks"].items():
    existing = hooks.setdefault(event, [])
    for group in groups:
        commands = {hook.get("command")
                    for present in existing for hook in present.get("hooks", [])}
        if any(hook["command"] in commands for hook in group["hooks"]):
            continue
        existing.append(group)
        added += 1

os.makedirs(os.path.dirname(target_path), exist_ok=True)
with open(target_path, "w", encoding="utf-8") as handle:
    json.dump(target, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
print("  %s: %d registration(s) added" % (target_path, added))
PYEOF
}

echo "Installed $TARGET/.speckit-hooks"
if [ "$AGENT" = "claude" ] || [ "$AGENT" = "both" ]; then
    merge "$TEMPLATE/.claude/settings.json" "$TARGET/.claude/settings.json"
fi
if [ "$AGENT" = "codex" ] || [ "$AGENT" = "both" ]; then
    merge "$TEMPLATE/.codex/hooks.json" "$TARGET/.codex/hooks.json"
    if [ ! -e "$TARGET/.codex/config.toml" ]; then
        printf '# Present so Codex loads this repository as a project config layer.\n' \
            > "$TARGET/.codex/config.toml"
    fi
fi

cat <<NOTE

Next steps
  1. Restart the CLI. Claude Code snapshots hook configuration at session start.
  2. Codex needs two things before it loads a project hook:
       a. the repository trusted as a persisted entry in \$CODEX_HOME/config.toml --
          open it once in the Codex TUI and accept the trust prompt;
       b. the hook approved with /hooks, which records its content hash. Editing the
          command string revokes that approval. The entry is keyed as:
            $TARGET/.codex/hooks.json:user_prompt_submit:<group>:<hook>
     Run scripts/probe-codex.sh to see which of the two is missing.
  3. Verify with: .speckit-hooks/speckit-hook --agent=claude </dev/null; echo \$?
     Exit status 0 with no output means the runner is installed and idle.
NOTE
