"""Per-CLI input and output contracts.

The two CLIs agree on the success envelope and disagree completely on how to
refuse. Claude blocks with exit status 2. Codex has no exit-2 path on
`UserPromptSubmit` at all — its output schema carries `decision: "block"` with a
non-empty `reason`, and the process must still exit 0. Copying Claude's behaviour
into the Codex adapter would make every block silently disappear, so the difference
is modelled explicitly rather than shared.
"""

import json
import sys

AGENTS = ("claude", "codex")


class Adapter(object):
    name = None

    def emit(self, route, text, stream=None):
        """Write the success envelope. `hookEventName` echoes the received event."""
        stream = sys.stdout if stream is None else stream
        payload = {"hookSpecificOutput": {
            "hookEventName": route.native_event,
            "additionalContext": text,
        }}
        stream.write(json.dumps(payload, ensure_ascii=False))
        stream.write("\n")
        stream.flush()

    def block(self, reason, out=None, err=None):
        """Refuse the request. Returns the process exit status to use."""
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    name = "claude"

    def block(self, reason, out=None, err=None):
        err = sys.stderr if err is None else err
        # On `PreToolUse` this text is shown to the model rather than the user, and a
        # model that merely sees an error may route around the gate. The instruction
        # to surface it is therefore part of the message, not the surrounding docs.
        err.write("Spec Kit pre-hook blocked this request.\n%s\n"
                  "Do not proceed with the Spec Kit stage; surface this to the user.\n"
                  % (reason,))
        err.flush()
        return 2


class CodexAdapter(Adapter):
    name = "codex"

    def emit(self, route, text, stream=None):
        stream = sys.stdout if stream is None else stream
        # Codex's output schema is `additionalProperties: false`, so the envelope
        # must carry nothing beyond these keys.
        payload = {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }}
        stream.write(json.dumps(payload, ensure_ascii=False))
        stream.write("\n")
        stream.flush()

    def block(self, reason, out=None, err=None):
        out = sys.stdout if out is None else out
        message = ("Spec Kit pre-hook blocked this request. %s "
                   "Do not proceed with the Spec Kit stage." % (reason,))
        # `reason` alone is not shown to the user in non-interactive `codex exec`,
        # which reports only "hook: UserPromptSubmit Blocked". `systemMessage` is the
        # channel that surfaces the explanation, so both carry it.
        out.write(json.dumps({"decision": "block", "reason": message,
                              "systemMessage": message}, ensure_ascii=False))
        out.write("\n")
        out.flush()
        return 0


def for_agent(agent):
    if agent == "claude":
        return ClaudeAdapter()
    if agent == "codex":
        return CodexAdapter()
    raise ValueError("unknown agent %r" % (agent,))
