# Guarantees and limits

This document states exactly what the pre-hook runner guarantees, what it cannot
guarantee, and which host behaviours were measured rather than assumed.

## Guaranteed

These hold whenever the native hook runs at all:

1. **The registry is read.** `.specify/extensions.yml` is parsed by a strict parser
   that refuses constructs outside the subset Spec Kit emits, so a file this tool
   cannot fully understand produces a block rather than a partial reading.
2. **The right event is selected.** Stage detection is anchored, so
   `before_taskstoissues` is never read as `before_tasks`, and near-miss names match
   nothing.
3. **Command ids resolve the way Spec Kit resolves them.** The transform is Spec
   Kit's own `CommandRegistrar._compute_output_name`, reimplemented as a pure
   function; no part of it depends on Spec Kit being importable at hook time.
4. **The real instruction body is loaded.** The file is opened, verified to be a
   regular file inside the project, split lexically, and delivered byte-for-byte,
   with its sha256 published next to it so the injected text can be checked against
   the file on disk.
5. **It reaches the current session.** The body is returned as `additionalContext`
   on the event that is already in flight. No second session, no subprocess, no
   agent spawn.
6. **A mandatory hook that cannot be loaded blocks the request.** Missing, empty,
   unreadable, escaping the project root, over budget, or naming an invalid command
   id — each refuses the stage rather than proceeding without the gate.

## Not guaranteed

1. **That the model carries out injected instructions faithfully.** The injected
   text is natural language. The runner guarantees delivery, not comprehension. This
   is the irreducible limit of the approach: no CLI currently exposes an API to
   enqueue a real skill invocation inside the session that is already running, so
   reading the command's prompt file and injecting its content is the strongest
   available mechanism. It is stated in the injected document, in the code, and here.
2. **That the user has the hook enabled.** Hooks are configuration. A user who
   removes the registration, declines to trust it, or runs the CLI with
   `--setting-sources` excluding the project layer gets no enforcement at all.
3. **Anything that does not pass through the hook.** Files edited by other tools, or
   stages driven from a different client, are outside the mechanism entirely.
4. **`after_*` hooks.** They stay model-dispatched. Only `before_*` is made
   deterministic.

## Measured host behaviour

### One event per entry point

A typed `/speckit-plan` fires `UserPromptExpansion` only; slash commands do not reach
`UserPromptSubmit`. A model-initiated skill call fires `PreToolUse` with
`tool_name: "Skill"` only. The two routes therefore cannot both inject for a single
action. Captured payloads confirm the field shapes, including that
`tool_input.args` is **absent** rather than empty when a skill is called with no
arguments.

A per-turn guard covers the remaining case — one turn reaching the runner twice —
keyed on `(agent, session_id, prompt_id | turn_id, event)`. `prompt_id` correlates one
user prompt with every subsequent event until the next prompt, so a legitimate re-run
of the same stage later in the session arrives with a new key and injects again. Live
payload capture confirms `prompt_id` is present on **both** Claude routes, which the
earlier attempt's `tool_use_id` key was not — that field exists only on `PreToolUse`
and changes per tool call, so it could not suppress anything.

**Mandatory hooks deliberately bypass that guard.** A long turn can compact an
earlier injection out of context, and a missing mandatory gate costs more than a
repeated one.

### The two Claude refusal paths are not equally strong

| Route | Exit 2 message goes to | Practical strength |
|---|---|---|
| `UserPromptExpansion` | the user only | Hard stop. The expansion does not happen. |
| `PreToolUse` | **the model** | Softer. The model sees an error and may retry or route around the gate. |

Because of that asymmetry, the block message itself carries the instruction *"Do not
proceed with the Spec Kit stage; surface this to the user."* Its wording is pinned by
a test, because it is load-bearing rather than decorative.

### Codex refuses differently from Claude

Codex's `UserPromptSubmit` output schema — extracted verbatim from the installed
binary into `tests/fixtures/schemas/` — has no exit-2 refusal. A refusal is
`{"decision":"block","reason":…}` on stdout with a **zero** exit status, and the
top-level object is `additionalProperties: false`, so an extra key invalidates the
whole document. Copying Claude's adapter would make every Codex block disappear
silently; the two are modelled separately and both are covered by schema-conformance
tests.

This is also why the entry point is POSIX `sh` rather than Python. When `python3` is
missing, the hook still has to refuse — and on Codex refusing means *printing JSON*,
which Python cannot do if Python is what is missing.

### Codex project hooks: two conditions, both required

Codex will only load a project-local `.codex/hooks.json` when **both** of the
following hold. Established experimentally against codex-cli 0.154.0 with an isolated
`CODEX_HOME`; `scripts/probe-codex.sh` re-runs the check and reports which condition
is missing.

1. **The repository is trusted, as a persisted entry** in `$CODEX_HOME/config.toml`:

   ```toml
   [projects."/absolute/path/to/repo"]
   trust_level = "trusted"
   ```

   Passing the same key as a `-c` override does **not** work. This is the condition
   that made an earlier investigation conclude, wrongly, that project hooks never
   load at all. Opening the repository in the Codex TUI once and accepting the trust
   prompt writes this entry.

2. **The hook's content is trusted**: either the correct `trusted_hash`, which the
   `/hooks` TUI writes when the hook is approved, or `--dangerously-bypass-hook-trust`
   for automation that has already vetted the script. A deliberately wrong hash is
   rejected, so the value is verified rather than merely required to be present.

Two things that are *not* conditions, contrary to reasonable expectation:

- **`enabled` is not one.** A hook with no `hooks.state` entry runs once the two
  conditions above are met; it does not default to disabled.
- **`codex exec` is not the problem.** The measurements above were all taken under
  `codex exec`, which emits `UserPromptSubmit` normally, and the working directory
  does not matter — `-C` from outside the repository behaves the same as running
  inside it.

Editing the command string changes its hash and revokes condition 2, so a project
hook stops running after any edit until it is re-approved.

Two further practical notes from the same measurements: the `command` string is
evaluated by a shell, so a path containing spaces must be quoted inside it; and
declaring the same hook in both `.codex/hooks.json` and `.codex/config.toml` causes it
to fire **twice**, which matches Codex's own warning about preferring a single
representation per layer.

Codex exposes no `CODEX_PROJECT_DIR`, so the project root is derived from the
payload's `cwd`.

**Verification status: confirmed end-to-end on both CLIs.**

Claude, via `scripts/verify-claude.sh`: a typed `/speckit-plan` produced exactly one
`UserPromptExpansion` and no `PreToolUse`; a model-initiated skill call produced
exactly one `PreToolUse` and no `UserPromptExpansion`. In both cases the injected
context carried the installed skill's real body, and the model performed the pre-hook
before the stage.

Codex, running the shipped artifact in a real repository with both conditions
satisfied:

| Case | Result |
|---|---|
| `$speckit-plan` with a resolvable mandatory hook | `hook: UserPromptSubmit Completed`, and Codex raised no complaint about the output document — the injection envelope was accepted |
| an unrelated prompt | `Completed` with no output; the runner passed through silently |
| `$speckit-plan` with the mandatory skill file removed | **`hook: UserPromptSubmit Blocked`** — Codex acted on the `decision: block` document and refused the prompt |

The third row is the important one: it shows the Codex refusal path works, which is
the mechanism that would silently vanish if the Claude adapter's exit-2 behaviour had
been copied.

Not observed on Codex: the model's own response to the injected context, because the
Codex account was at its API usage limit throughout. Hooks run before the model
request, so everything above was still measurable. That gap is the same
model-behaviour limitation listed under "Not guaranteed", not a delivery question.

If a future Codex version stops loading project layers, the fallback is to register the
single entry in `~/.codex/hooks.json` instead: the runner derives the project root from
the payload's `cwd`, so one user-level registration covers every repository, and that
layer was confirmed to work here with no project trust at all.

### Size budget

The default budget is 64 KiB of injected text, overridable with
`SPECKIT_PREHOOK_MAX_BYTES`. A self-imposed limit keeps behaviour deterministic
regardless of the hosts' own undocumented ceilings. Mandatory bodies are never
truncated — over budget is a block — because a half-injected gate that the model
believes it satisfied is the worst available outcome. Optional offers are dropped
from the end with a count reported in the diagnostics section.

## Deliberate divergences from Spec Kit

| Behaviour | Spec Kit | Here | Why |
|---|---|---|---|
| Schema violations (non-list event, non-mapping entry, blank `command`) | silently normalized away | **block** | Normalizing could erase a mandatory gate without anyone noticing. |
| Duplicate YAML keys | PyYAML accepts, last value wins | **block** | A duplicated `optional:` silently changing a gate's strength is not acceptable. |
| Hook ordering | `get_hooks_for_event()` sorts by `priority`; templates do not sort | declaration order, with a warning when the two disagree | Declaration order is what a model actually sees today. |
| `enabled: "false"` as a string | truthy, therefore enabled | same, with a warning | Diverging would make the two disagree about which gates exist. |
| Optional hook bodies | not loaded | not loaded, but the file is resolved and reported | Matches the "offer, do not run" semantics while still surfacing a broken install. |

## Things deliberately not done

- **`condition` is not evaluated.** Spec Kit's `_evaluate_condition()` exists, but the
  command templates instruct the model not to evaluate conditions and to skip such
  hooks, and upstream's extension template calls the field a future feature.
  Evaluating it would make a hook fire that Spec Kit would not fire. Skipped hooks are
  listed with their condition text, so the skip is visible.
- **`agents/openai.yaml` is never modified.** Codex supports
  `policy.allow_implicit_invocation: false` to stop a skill being auto-selected by
  the model, and Spec Kit does not emit that file. It is worth knowing about, but
  writing into an extension's installed metadata is not this tool's business.
- **No `specify-cli` import.** The command-to-file rule is a pure function, so
  depending on Spec Kit internals would add a fragile version coupling for nothing.
