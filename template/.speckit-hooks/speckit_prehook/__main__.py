"""Entry point: read a hook payload, resolve the stage's pre-hooks, emit or block.

Exit status is the contract with the host and differs by adapter:

* `0` — either nothing to say, or a successful injection, or (on Codex) a refusal,
  which travels in the JSON document rather than the status.
* `2` — a Claude refusal.
* `1` — the runner itself could not work (unreadable stdin, internal fault). This
  never blocks the user's request; a bug in this tool must not hold a repository
  hostage.

A refusal and an injection are mutually exclusive. Nothing here ever writes
`additionalContext` and then blocks, or the model would act on a half-built list.
"""

import json
import os
import sys

if __package__ in (None, ""):  # `python .speckit-hooks/speckit_prehook` without a package
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "speckit_prehook"

from . import adapters
from . import config as config_module
from . import dedup
from . import render as render_module
from . import resolver
from . import route as route_module

MAX_BYTES_ENV = "SPECKIT_PREHOOK_MAX_BYTES"

EXIT_OK = 0
EXIT_RUNNER_FAULT = 1


class Usage(Exception):
    pass


def parse_args(argv):
    agent = None
    max_bytes = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--agent" and index + 1 < len(argv):
            agent = argv[index + 1]
            index += 2
            continue
        if token.startswith("--agent="):
            agent = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--max-bytes" and index + 1 < len(argv):
            max_bytes = argv[index + 1]
            index += 2
            continue
        if token.startswith("--max-bytes="):
            max_bytes = token.split("=", 1)[1]
            index += 1
            continue
        raise Usage("unrecognized argument %r" % (token,))
    if agent not in adapters.AGENTS:
        raise Usage("--agent must be one of %s" % (", ".join(adapters.AGENTS),))
    return agent, max_bytes


def _max_bytes(explicit, env):
    raw = explicit if explicit is not None else env.get(MAX_BYTES_ENV)
    if raw is None:
        return render_module.DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return render_module.DEFAULT_MAX_BYTES
    return value if value > 0 else render_module.DEFAULT_MAX_BYTES


def _describe_failure(resolved, event):
    return "`%s` (hooks.%s[%d]): %s" % (
        resolved.entry.command, event, resolved.entry.index,
        resolved.detail or resolved.status)


def run(argv, stdin, stdout, stderr, env):
    try:
        agent, max_bytes_arg = parse_args(argv)
    except Usage as error:
        stderr.write("speckit-prehook: %s\n" % (error,))
        return EXIT_RUNNER_FAULT
    adapter = adapters.for_agent(agent)

    raw = stdin.read()
    if not raw.strip():
        return EXIT_OK
    try:
        payload = json.loads(raw)
    except ValueError as error:
        stderr.write("speckit-prehook: hook payload was not valid JSON: %s\n" % (error,))
        return EXIT_RUNNER_FAULT
    if not isinstance(payload, dict):
        stderr.write("speckit-prehook: hook payload must be a JSON object\n")
        return EXIT_RUNNER_FAULT

    current = route_module.build_route(agent, payload)
    if current is None:
        return EXIT_OK

    root = resolver.find_project_root(current.cwd, env)
    if root is None:
        return EXIT_OK

    config_path = os.path.join(root, ".specify", "extensions.yml")
    try:
        registry = config_module.load(config_path)
        if registry is None:
            return EXIT_OK
        collected = config_module.collect(registry.data, current.registry_event)
    except config_module.RegistryError as error:
        return adapter.block(
            "%s No hooks were checked for %s, including any mandatory "
            "(`optional: false`) hooks registered there."
            % (error, current.registry_event), out=stdout, err=stderr)

    if collected.is_empty:
        return EXIT_OK

    mandatory = [resolver.resolve(root, agent, entry) for entry in collected.mandatory]
    failures = [item for item in mandatory if not item.ok]
    if failures:
        detail = "; ".join(_describe_failure(item, current.registry_event)
                           for item in failures)
        return adapter.block(
            "A mandatory pre-hook for %s could not be loaded, so it cannot be "
            "guaranteed to run: %s" % (current.registry_event, detail),
            out=stdout, err=stderr)

    optional = [resolver.resolve(root, agent, entry) for entry in collected.optional]

    # Mandatory hooks deliberately bypass dedup: a long turn can compact an earlier
    # injection out of context, and a missing gate costs more than a repeated one.
    claim = dedup.Claim(granted=True)
    if not mandatory:
        claim = dedup.claim(root, current, env)
        if not claim.granted:
            return EXIT_OK

    context = render_module.Context(
        route=current, root=root, config_sha=registry.sha256, collected=collected,
        mandatory=mandatory, optional=optional,
        max_bytes=_max_bytes(max_bytes_arg, env))
    try:
        document = render_module.render(context)
    except render_module.OverBudget as error:
        claim.release()
        return adapter.block(str(error), out=stdout, err=stderr)

    if not document:
        claim.release()
        return EXIT_OK

    adapter.emit(current, document, stream=stdout)
    claim.finalize({
        "agent": agent,
        "event": current.registry_event,
        "session_id": current.session_id,
        "turn_key": current.turn_key,
        "mandatory": [item.entry.command for item in mandatory],
        "optional": [item.entry.command for item in optional],
    })
    return EXIT_OK


def main():
    try:
        return run(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr, os.environ)
    except Exception as error:  # a runner fault must not block the user's request
        sys.stderr.write("speckit-prehook: internal error: %s: %s\n"
                         % (type(error).__name__, error))
        return EXIT_RUNNER_FAULT


if __name__ == "__main__":
    sys.exit(main())
