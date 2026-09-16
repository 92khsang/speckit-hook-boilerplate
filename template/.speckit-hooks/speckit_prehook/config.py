"""Reading and classifying `.specify/extensions.yml`.

Field defaults and truthiness mirror Spec Kit's own reader
(`HookExecutor.get_project_config` / `get_hooks_for_event`) exactly, including the
parts that look like bugs. A string `enabled: "false"` is truthy in Python and
therefore leaves the hook *enabled*; "fixing" that here would make this hook and
Spec Kit disagree about which gates exist, which is worse than the oddity itself.

Where this module deliberately differs from Spec Kit is in what it does with a
schema violation. Spec Kit silently normalizes a non-list event value to `[]` and
drops non-dict entries; that is safe for a renderer, but here it could erase a
mandatory gate without anyone noticing, so those cases raise `RegistryError` and
block the request instead.
"""

import hashlib

from . import miniyaml

DEFAULT_PRIORITY = 10

MANDATORY = "mandatory"
OPTIONAL = "optional"
DISABLED = "disabled"
SKIPPED_CONDITION = "condition"


class RegistryError(Exception):
    """The registry cannot be trusted, so the request must be blocked."""


class HookEntry(object):
    """One registration under `hooks.<event>`, normalized but not resolved."""

    __slots__ = ("index", "extension", "command", "enabled", "optional", "priority",
                 "prompt", "description", "condition", "kind", "notes")

    def __init__(self, index, raw):
        self.index = index
        self.notes = []
        self.extension = _string(raw.get("extension"))
        self.command = raw.get("command")
        self.enabled = bool(raw.get("enabled", True))
        self.optional = bool(raw.get("optional", True))
        self.priority = normalize_priority(raw.get("priority"), DEFAULT_PRIORITY)
        self.description = _string(raw.get("description"))
        self.condition = raw.get("condition")
        command_text = self.command if isinstance(self.command, str) else ""
        self.prompt = _string(raw.get("prompt")) or ("Execute %s?" % command_text)
        self.kind = None

        if "enabled" in raw and not isinstance(raw.get("enabled"), bool):
            self.notes.append(
                "`enabled` is %r, not a boolean; Spec Kit evaluates it for truthiness, "
                "so this hook counts as %s"
                % (raw.get("enabled"), "enabled" if self.enabled else "disabled"))
        if "optional" in raw and not isinstance(raw.get("optional"), bool):
            self.notes.append(
                "`optional` is %r, not a boolean; treated as %s"
                % (raw.get("optional"), "optional" if self.optional else "mandatory"))


class Collected(object):
    """The classified contents of one `hooks.<event>` list."""

    __slots__ = ("event", "entries", "mandatory", "optional", "skipped",
                 "disabled_count", "total", "notes")

    def __init__(self, event):
        self.event = event
        self.entries = []
        self.mandatory = []
        self.optional = []
        self.skipped = []
        self.disabled_count = 0
        self.total = 0
        self.notes = []

    @property
    def is_empty(self):
        return not self.mandatory and not self.optional and not self.skipped


def _string(value):
    return value if isinstance(value, str) else ""


def normalize_priority(value, default=DEFAULT_PRIORITY):
    """Mirror Spec Kit's `normalize_priority`: ints >= 1, everything else default."""
    if isinstance(value, bool):
        return default
    try:
        priority = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return priority if priority >= 1 else default


class Registry(object):
    """A parsed registry plus the digest of the exact bytes it was parsed from."""

    __slots__ = ("data", "sha256")

    def __init__(self, data, sha256):
        self.data = data
        self.sha256 = sha256


def load(config_path):
    """Parse the registry file.

    Returns a `Registry`, or `None` when the file does not exist — that is the "no
    hooks registered" case and must pass silently.

    Raises:
        RegistryError: the file exists but cannot be read or understood. Spec Kit's
            own command templates require this to be reported rather than skipped,
            because an unreadable registry may be hiding a mandatory hook.
    """
    try:
        with open(str(config_path), "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise RegistryError("%s is a directory, not a file" % (config_path,))
    except OSError as error:
        raise RegistryError("cannot read %s: %s" % (config_path, error))

    digest = hashlib.sha256(raw).hexdigest()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RegistryError("%s is not valid UTF-8: %s" % (config_path, error))

    try:
        data = miniyaml.parse(text)
    except miniyaml.YAMLError as error:
        raise RegistryError("%s could not be parsed: %s" % (config_path, error))

    if data is None:
        return Registry({}, digest)
    if not isinstance(data, dict):
        raise RegistryError(
            "%s must contain a mapping at the top level, found %s"
            % (config_path, type(data).__name__))
    return Registry(data, digest)


def collect(config, event):
    """Classify the registrations for one `before_<stage>` event.

    Entries keep their YAML declaration order, which is the order Spec Kit's command
    templates present them in. `priority` is carried through for display and a
    mismatch between the two orderings is reported, but it does not reorder anything.

    Raises:
        RegistryError: the `hooks` key or this event's value violates the schema in
            a way that could conceal a registration.
    """
    result = Collected(event)
    if not config:
        return result

    hooks = config.get("hooks")
    if hooks is None:
        return result
    if not isinstance(hooks, dict):
        raise RegistryError(
            "`hooks` must be a mapping of event name to a list of registrations, "
            "found %s" % (type(hooks).__name__,))

    raw_entries = hooks.get(event)
    if raw_entries is None:
        return result
    if not isinstance(raw_entries, list):
        raise RegistryError(
            "`hooks.%s` must be a list of registrations, found %s"
            % (event, type(raw_entries).__name__))

    result.total = len(raw_entries)
    seen_commands = {}
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise RegistryError(
                "`hooks.%s[%d]` must be a mapping, found %s"
                % (event, index, type(raw).__name__))
        entry = HookEntry(index, raw)
        if not isinstance(entry.command, str) or not entry.command.strip():
            raise RegistryError(
                "`hooks.%s[%d]` has no usable `command` (found %r)"
                % (event, index, entry.command))
        entry.command = entry.command.strip()

        # Spec Kit's writer de-duplicates by command with last-wins, so a reader that
        # kept both copies would run a gate twice.
        previous = seen_commands.get(entry.command)
        if previous is not None:
            previous.kind = None
            result.entries.remove(previous)
            result.notes.append(
                "`%s` is registered more than once under `hooks.%s`; Spec Kit keeps "
                "the last registration, so registration %d supersedes %d"
                % (entry.command, event, index, previous.index))
        seen_commands[entry.command] = entry

        if not entry.enabled:
            entry.kind = DISABLED
        elif isinstance(entry.condition, str) and entry.condition.strip():
            entry.kind = SKIPPED_CONDITION
        elif entry.condition is not None and not isinstance(entry.condition, str):
            entry.kind = SKIPPED_CONDITION
        else:
            entry.kind = MANDATORY if not entry.optional else OPTIONAL
        result.entries.append(entry)

    for entry in result.entries:
        if entry.kind == DISABLED:
            result.disabled_count += 1
        elif entry.kind == SKIPPED_CONDITION:
            result.skipped.append(entry)
        elif entry.kind == MANDATORY:
            result.mandatory.append(entry)
        else:
            result.optional.append(entry)

    active = result.mandatory + result.optional
    if _priority_order_differs(active):
        result.notes.append(
            "declaration order and `priority` order differ for `hooks.%s`; this list "
            "follows declaration order, which is what Spec Kit's command templates "
            "use. `HookExecutor.get_hooks_for_event()` would sort by priority."
            % (event,))
    return result


def _priority_order_differs(entries):
    if len(entries) < 2:
        return False
    by_declaration = [entry.index for entry in sorted(entries, key=lambda e: e.index)]
    by_priority = [entry.index for entry in
                   sorted(entries, key=lambda e: (e.priority, e.index))]
    return by_declaration != by_priority
