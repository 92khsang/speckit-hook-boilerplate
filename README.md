# Deterministic Spec Kit pre-hooks

Spec Kit extensions register `hooks.before_<stage>` in `.specify/extensions.yml`, but
Spec Kit never executes them. `HookExecutor.execute_hook()` only *returns a descriptor*;
the real dispatcher is prose inside `templates/commands/*.md` telling the model to read
the registry and run the hook itself. A `before_implement` gate is therefore exactly as
reliable as the model's willingness to follow instructions.

This boilerplate moves that dispatch decision out of the model and into each CLI's own
hook system. On every Spec Kit stage invocation the native hook:

1. identifies the stage from the CLI's hook payload,
2. reads `.specify/extensions.yml`,
3. resolves each enabled `hooks.before_<stage>` registration's `command` to the
   installed skill file,
4. reads that file's real instruction body, and
5. returns it to the **current** session as `additionalContext`, in declaration order,
   with an explicit execution contract.

It never executes hook content and never starts a nested `claude` or `codex` process.
`after_*` hooks remain model-dispatched.

## Requirements

- `python3` 3.9 or newer. Nothing else — no PyYAML, no `jq`, no `yq`, no virtualenv.
  The YAML subset Spec Kit emits is parsed by a strict in-tree parser that refuses
  anything outside that subset rather than guessing.
- POSIX `sh`. Tested on Ubuntu; written to avoid GNU-only behaviour (`pwd -P` instead
  of `readlink -f`, no `sed`/`mktemp` flag differences) so it also runs on macOS.
- Spec Kit v1.0.7 or compatible, installed for Claude Code (`.claude/skills/`) or
  Codex CLI (`.agents/skills/`).

## Install

```sh
scripts/install.sh --target /path/to/your/repo          # both CLIs
scripts/install.sh --target /path/to/your/repo --agent claude
```

This copies `.speckit-hooks/` into the target repository and merges the hook
registrations into `.claude/settings.json` and `.codex/hooks.json`, leaving any hooks
already configured there alone. Commit all of it.

Then **restart the CLI session** — Claude Code snapshots hook configuration at session
start.

Codex needs two more things before it will load a project-local hook, and both were
measured rather than assumed:

1. the repository must be trusted as a *persisted* entry in `$CODEX_HOME/config.toml`
   (opening it once in the Codex TUI and accepting the trust prompt writes this); and
2. the hook must be approved with `/hooks` in the Codex TUI, which records its content
   hash. Editing the command string revokes that approval.

`scripts/probe-codex.sh` reports which of the two is missing. Details and the
supporting measurements are in
[docs/guarantees.md](docs/guarantees.md#codex-project-hooks-two-conditions-both-required).
Both CLI paths, including the Codex refusal path, have been confirmed end-to-end.

Verify the runner is wired up:

```sh
.speckit-hooks/speckit-hook --agent=claude </dev/null; echo $?
```

Exit status `0` with no output means installed and idle.

## How stages are detected

| CLI | Event | Matched against |
|---|---|---|
| Claude Code | `UserPromptExpansion` | `command_name`, for a typed `/speckit-plan` |
| Claude Code | `PreToolUse` (`tool_name: Skill`) | `tool_input.skill`, for a model-initiated skill call |
| Codex CLI | `UserPromptSubmit` | a leading `$speckit-plan` in `prompt` |

A typed slash command fires `UserPromptExpansion` only — slash commands never reach
`UserPromptSubmit` — and a model-initiated skill call fires `PreToolUse` only, so the
two Claude routes cannot both inject for one action. A per-turn guard keyed on
`prompt_id` / `turn_id` covers the remaining case where one turn reaches the runner
twice.

Matching is anchored: `speckit-tasks` and `speckit-taskstoissues` are distinct, and
`speckit-implementation`, `speckit-implement-extra` and `speckit-compound-check` match
nothing. On Codex, only a line-leading `$speckit-<stage>` counts, so
`Explain $speckit-implement` and a backticked mention are ignored. A false positive
would inject a mandatory gate into an unrelated conversation; a false negative only
falls back to Spec Kit's own template-driven discovery.

The `matcher` values in the shipped configurations are deliberately permissive. All
accuracy lives in one unit-tested function rather than being split across two config
files and two matcher dialects. Note that a Claude matcher containing only
`[A-Za-z0-9_- ,|]` is an *exact* match while anything else is an unanchored regex —
`"speckit-"` would match nothing at all, which is why the shipped value is
`"speckit[-.]"`.

## Spec Kit semantics this preserves

Field names, defaults and truthiness mirror `HookExecutor.register_hooks()` and
`get_hooks_for_event()`:

| Field | Behaviour |
|---|---|
| `command` | Required. Resolved by Spec Kit's own rule: strip `speckit.`, dots to hyphens, prefix `speckit-`. |
| `enabled` | Absent means enabled. Evaluated for truthiness, so a *string* `"false"` leaves the hook enabled — reported, not "fixed", because diverging from Spec Kit would be worse. |
| `optional` | Absent means `true`. `false` means the agent must run it and wait; `true` means print an offer and let the user decide. |
| `priority` | Read and displayed, but not used for ordering. |
| `condition` | A non-empty condition means the hook is **skipped and reported**. |
| `prompt`, `description` | Shown on optional offers only. |
| `extension` | Cross-checked against the command id and the installed manifest; a mismatch is a warning. |

**Ordering is YAML declaration order.** `HookExecutor.get_hooks_for_event()` sorts by
`priority`, but the command templates do not, and the official documentation states
templates use the configured YAML order — so declaration order is what a model sees
today. When the two orders disagree, the injected document says so.

**`condition` is not evaluated.** Spec Kit ships a `_evaluate_condition()` mini-DSL,
but its command templates explicitly instruct the model to *"not attempt to interpret
or evaluate hook `condition` expressions"* and to skip such hooks, and upstream's own
extension template calls the field a future feature. Evaluating it here would make a
hook fire that Spec Kit would not fire. Skipped hooks are listed with their condition
so the skip is never silent.

**Optional bodies are not injected.** An optional hook is an offer. Injecting
instructions the model is simultaneously told not to follow wastes budget and creates
an injection surface. The offer block, the resolved path and the file's hash are
reported instead; the body is loaded only for mandatory hooks, which is where
determinism is required.

## Failure policy

The refusal mechanism differs per route, and getting this wrong is the easiest way to
lose a gate silently:

| Route | How it refuses | Exit status |
|---|---|---|
| Claude `UserPromptExpansion` | exit 2, message to the **user** | 2 |
| Claude `PreToolUse` | exit 2, message to the **model** | 2 |
| Codex `UserPromptSubmit` | `{"decision":"block","reason":…}` on stdout | **0** |

| Condition | Result |
|---|---|
| No registry, no `hooks`, empty event list, stage not matched | pass silently |
| Malformed or unsupported YAML in `extensions.yml`, including duplicate keys | **block** |
| Schema violation: non-list event, non-mapping entry, missing or blank `command` | **block** |
| Mandatory command id invalid, file missing, unreadable, or resolving outside the project | **block** |
| Mandatory body empty, or over the size budget | **block** |
| `python3` missing or older than 3.9 | **block**, from the shell shim |
| Optional hook unresolvable | warn in the diagnostics section |
| Non-empty `condition`, even with `optional: false` | reported as skipped |
| Unreadable stdin, or an internal fault in the runner | exit 1, request untouched |

A block never also emits `additionalContext`. Mandatory bodies are never truncated: a
half-injected gate that the model believes it satisfied is worse than a refusal.

## What is and is not guaranteed

Guaranteed: reading the registry, selecting `before_<stage>`, resolving command ids to
installed files, loading the real prompt body, injecting it into the current session,
and blocking the request when a mandatory hook cannot be loaded.

Not guaranteed: that the model carries out injected natural-language instructions
faithfully; enqueuing a real skill invocation in the same session, which no CLI API
exposes; behaviour when the user disables or declines to trust the hook. Full detail,
including the asymmetry between the two Claude refusal paths, is in
[docs/guarantees.md](docs/guarantees.md).

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

Stdlib only, no network. Fixtures are built under paths containing spaces and
non-ASCII characters and are exercised from nested directories and real git
worktrees. The suite covers the registry matrix, resolution, frontmatter edge cases,
literal injection of hostile bodies, the full route accept/reject table, both refusal
mechanisms, runtime absence, deduplication, and conformance against JSON schemas
extracted from the Codex binary. `scripts/verify-claude.sh` runs a live end-to-end
check; `scripts/probe-codex.sh` diagnoses Codex hook delivery.

## Migrating from an earlier `.agent/hooks/` version

See [docs/migration.md](docs/migration.md).
