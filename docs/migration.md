# Migrating from an earlier `.agent/hooks/` installation

An earlier generation of this idea installed under `.agent/hooks/` and shipped in two
shapes:

- a Bash dispatcher (`speckit-before.sh`) calling a hand-written policy file
  (`before.sh`) that hard-coded `.specify/scripts/bash/check-prerequisites.sh
  --require-tasks`, and
- a Python version (`inject.py` plus a `.venv`) that pinned `specify-cli` to a git
  commit and called its private APIs.

Nothing is carried over. The command paths, the CLI argument, the state location and
the runtime dependencies all changed, and the hard-coded prerequisite check is
removed rather than preserved — running a fixed check was never the point of this
mechanism, and keeping it would have obscured what the hook actually does.

## Steps

1. **Remove the old installation.**

   ```sh
   rm -rf .agent/hooks
   ```

   That includes `before.sh`, `speckit-before.sh`, `inject.py`, `requirements.txt`,
   any `.venv/`, any `.state/` and any `__pycache__/`. If `.agent/` is then empty,
   remove it too — note that it is easily confused with Codex's own `.agents/`
   directory, which must be left alone.

2. **Remove the old registrations** from `.claude/settings.json` and
   `.codex/hooks.json`. They are the entries whose `command` mentions
   `.agent/hooks/`. Leave any unrelated hooks in place.

3. **Install the new runner.**

   ```sh
   scripts/install.sh --target .
   ```

4. **Restart the CLI session.** Claude Code snapshots hook configuration at session
   start. For Codex, run `/hooks` and approve the new `UserPromptSubmit` entry; the
   old entry's approval does not transfer, because trust is keyed to the hash of the
   command string.

5. **Drop the `specify-cli` pin** if you added one for the previous version. The new
   runner needs `python3` 3.9+ and nothing else.

## If you had customised the old policy file

The old `before.sh` was a place to hand-write repository checks. There is no
equivalent here, and that is deliberate: this runner's job is to make the hooks
*Spec Kit already knows about* fire deterministically, not to be a second,
parallel place where checks live.

Move such a check into a Spec Kit extension command and register it the normal way:

```yaml
# .specify/extensions/<your-extension>/extension.yml
hooks:
  before_implement:
    command: speckit.<your-extension>.preflight
    optional: false
    description: Repository preflight checks
```

Install the extension with `specify extension add`, which writes the registration
into `.specify/extensions.yml`. The runner then resolves
`speckit.<your-extension>.preflight` to its installed `SKILL.md` and injects that
file's instructions before the implement stage — which is the behaviour the old
hard-coded policy was approximating.

## Behaviour changes worth knowing

| | Old | New |
|---|---|---|
| What runs | a shell policy file, executed by the hook | nothing is executed; instruction text is injected |
| Source of truth | the policy file | `.specify/extensions.yml` |
| Runtime | bash + jq, or a pinned `specify-cli` in a venv | `python3` 3.9+ only |
| Dedup state | `.agent/hooks/.state/` inside the repository | `$XDG_STATE_HOME/speckit-prehook/`, outside it |
| `optional: true` | not modelled | offered to the user, not run |
| `condition` | not modelled | skipped and reported, matching Spec Kit |

The state directory moved out of the repository on purpose: a state file inside the
working tree shows up in `git status`, wakes file watchers, and fails outright in a
read-only CI checkout.
