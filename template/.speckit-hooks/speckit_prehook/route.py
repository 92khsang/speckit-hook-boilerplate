"""Stage detection.

The only place that decides which Spec Kit stage a hook payload refers to.
Everything here is pure: `extract_stage()` takes a decoded payload and returns a
`Route` or `None`. Keeping the authoritative matching in one pure function is what
lets the CLI-side `matcher` settings stay deliberately permissive.
"""

import re

# Longest-first alternation. A prefix match would let `speckit-tasks` swallow
# `speckit-taskstoissues` and read the wrong registry event, so every pattern below
# is anchored and the alternation is ordered longest-first for good measure.
STAGES = (
    "taskstoissues",
    "constitution",
    "implement",
    "checklist",
    "converge",
    "specify",
    "clarify",
    "analyze",
    "tasks",
    "plan",
)
_ALT = "|".join(STAGES)

# Claude routes match a command/skill name. Spec Kit installs Claude commands as
# skills named `speckit-<stage>`; the dotted spelling is accepted too so the same
# constant covers command-layout agents.
STAGE_EXACT_RE = re.compile(r"^/?speckit[-.](" + _ALT + r")$")

# Codex matches the prompt text. The rule deliberately mirrors what Codex itself
# does, which was measured rather than assumed: it expands a `$skill` token wherever
# it appears, including mid-sentence and inside markdown code spans. Restricting this
# to the start of a line, or ignoring backticked mentions, would let a real stage
# invocation run with its mandatory gate skipped.
#
# Boundaries on both sides keep `$speckit-implementation`, `$speckit-implement-extra`
# and `x$speckit-plan` from matching.
CODEX_INVOCATION_RE = re.compile(
    r"(?<![0-9A-Za-z_$-])\$speckit[-.](" + _ALT + r")(?![0-9A-Za-z_-])"
)

CLAUDE_EVENTS = ("UserPromptExpansion", "PreToolUse")
CODEX_EVENTS = ("UserPromptSubmit",)


class Route(object):
    """What a matched payload tells us, in the terms the rest of the code uses."""

    __slots__ = ("agent", "native_event", "stage", "registry_event", "other_stages",
                 "session_id", "turn_key", "cwd")

    def __init__(self, agent, native_event, stage, session_id, turn_key, cwd,
                 other_stages=()):
        self.agent = agent
        self.native_event = native_event
        self.stage = stage
        self.registry_event = "before_" + stage
        # Further stages named in the same prompt. Only the first is resolved, so
        # naming the rest keeps the partial coverage visible instead of silent.
        self.other_stages = tuple(other_stages)
        self.session_id = session_id
        self.turn_key = turn_key
        self.cwd = cwd

    @property
    def invocation_prefix(self):
        return "$" if self.agent == "codex" else "/"


def _text(value):
    return value if isinstance(value, str) else ""


def extract_stages(agent, payload):
    """Return the Spec Kit stages this payload refers to, in order of appearance.

    `agent` is the explicit `--agent` value; payload sniffing is not used because
    `UserPromptSubmit` exists in both CLIs and would be ambiguous.
    """
    if not isinstance(payload, dict):
        return []
    event = _text(payload.get("hook_event_name"))

    if agent == "claude":
        if event == "UserPromptExpansion":
            # `mcp_prompt` expansions are a different namespace and are not Spec Kit.
            if payload.get("expansion_type") != "slash_command":
                return []
            match = STAGE_EXACT_RE.match(_text(payload.get("command_name")))
            return [match.group(1)] if match else []
        if event == "PreToolUse":
            if payload.get("tool_name") != "Skill":
                return []
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                return []
            # `args` is omitted entirely when empty, so never index it.
            match = STAGE_EXACT_RE.match(_text(tool_input.get("skill")))
            return [match.group(1)] if match else []
        return []

    if agent == "codex":
        if event != "UserPromptSubmit":
            return []
        found = []
        for match in CODEX_INVOCATION_RE.finditer(_text(payload.get("prompt"))):
            if match.group(1) not in found:
                found.append(match.group(1))
        return found

    return []


def extract_stage(agent, payload):
    """The first stage this payload refers to, or None."""
    stages = extract_stages(agent, payload)
    return stages[0] if stages else None


def build_route(agent, payload):
    stages = extract_stages(agent, payload)
    if not stages:
        return None
    stage = stages[0]
    # `prompt_id` correlates one user prompt with every event until the next one,
    # which is exactly the dedup scope we want, and it exists on both Claude routes.
    turn_key = payload.get("prompt_id") or payload.get("turn_id")
    return Route(
        agent=agent,
        native_event=_text(payload.get("hook_event_name")),
        stage=stage,
        session_id=payload.get("session_id"),
        turn_key=turn_key if isinstance(turn_key, str) else None,
        cwd=_text(payload.get("cwd")) or None,
        other_stages=stages[1:],
    )
