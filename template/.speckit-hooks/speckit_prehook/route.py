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

# Codex matches the prompt text. Anchored at line start: a false positive would
# inject a mandatory gate into an unrelated conversation, whereas a false negative
# only degrades to Spec Kit's own template-driven discovery.
CODEX_LEADING_RE = re.compile(
    r"^[ \t]*\$speckit[-.](" + _ALT + r")(?![0-9A-Za-z_-])", re.MULTILINE
)

CLAUDE_EVENTS = ("UserPromptExpansion", "PreToolUse")
CODEX_EVENTS = ("UserPromptSubmit",)

_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+).*?(?:\n)(?:.*?\n)*?[ \t]*\1[ \t]*$", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


class Route(object):
    """What a matched payload tells us, in the terms the rest of the code uses."""

    __slots__ = ("agent", "native_event", "stage", "registry_event",
                 "session_id", "turn_key", "cwd")

    def __init__(self, agent, native_event, stage, session_id, turn_key, cwd):
        self.agent = agent
        self.native_event = native_event
        self.stage = stage
        self.registry_event = "before_" + stage
        self.session_id = session_id
        self.turn_key = turn_key
        self.cwd = cwd

    @property
    def invocation_prefix(self):
        return "$" if self.agent == "codex" else "/"


def _text(value):
    return value if isinstance(value, str) else ""


def strip_code_spans(prompt):
    """Remove fenced blocks and inline code so a quoted mention is not an invocation.

    `` `$speckit-implement` `` in a question about the command must not trigger a
    mandatory gate. Replacing with spaces preserves offsets and line structure.
    """
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return _INLINE_CODE_RE.sub(blank, _FENCE_RE.sub(blank, prompt))


def extract_stage(agent, payload):
    """Return the Spec Kit stage this payload refers to, or None.

    `agent` is the explicit `--agent` value; payload sniffing is not used because
    `UserPromptSubmit` exists in both CLIs and would be ambiguous.
    """
    if not isinstance(payload, dict):
        return None
    event = _text(payload.get("hook_event_name"))

    if agent == "claude":
        if event == "UserPromptExpansion":
            # `mcp_prompt` expansions are a different namespace and are not Spec Kit.
            if payload.get("expansion_type") != "slash_command":
                return None
            match = STAGE_EXACT_RE.match(_text(payload.get("command_name")))
            return match.group(1) if match else None
        if event == "PreToolUse":
            if payload.get("tool_name") != "Skill":
                return None
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                return None
            # `args` is omitted entirely when empty, so never index it.
            match = STAGE_EXACT_RE.match(_text(tool_input.get("skill")))
            return match.group(1) if match else None
        return None

    if agent == "codex":
        if event != "UserPromptSubmit":
            return None
        match = CODEX_LEADING_RE.search(strip_code_spans(_text(payload.get("prompt"))))
        return match.group(1) if match else None

    return None


def build_route(agent, payload):
    stage = extract_stage(agent, payload)
    if stage is None:
        return None
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
    )
