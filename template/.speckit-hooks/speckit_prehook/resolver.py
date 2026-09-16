"""Turning a hook's `command` ID into the installed skill file it names.

The transform is not guesswork. It reproduces Spec Kit's own
`CommandRegistrar._compute_output_name`: strip a leading `speckit.`, replace the
remaining dots with hyphens, prefix `speckit-`. Claude installs the result under
`.claude/skills/<name>/SKILL.md` and Codex under `.agents/skills/<name>/SKILL.md`.
Because the rule is a pure function of the command ID, none of this needs Spec Kit
to be importable at hook time.

`COMMAND_ID_RE` is the only gate between a value read out of YAML and a filesystem
path. Nothing here builds a path from a string that has not matched it.
"""

import os
import re

from . import miniyaml
from . import skilldoc

# Spec Kit's `EXTENSION_COMMAND_NAME_PATTERN`. The middle segment is the extension id.
COMMAND_ID_RE = re.compile(r"^speckit\.[a-z0-9-]+\.[a-z0-9-]+$")
SKILL_NAME_RE = re.compile(r"^speckit-[a-z0-9-]+$")

SKILL_DIRS = {
    "claude": os.path.join(".claude", "skills"),
    "codex": os.path.join(".agents", "skills"),
}
# Retired output locations. They are probed only to produce a better diagnostic;
# current Spec Kit does not install to either, so they are never resolved from.
LEGACY_HINTS = {
    "claude": os.path.join(".claude", "commands", "%s.md"),
    "codex": os.path.join(".codex", "prompts", "%s.md"),
}

MAX_WALK_UP = 32

OK = "ok"
OK_CROSS_AGENT = "ok_cross_agent"
INVALID_COMMAND = "invalid_command"
NOT_FOUND = "not_found"
EMPTY_BODY = "empty_body"


class ResolvedHook(object):
    """A registration paired with whatever the filesystem had to say about it."""

    __slots__ = ("entry", "skill_name", "status", "detail", "doc", "searched",
                 "found_agent", "notes")

    def __init__(self, entry, skill_name=None, status=NOT_FOUND, detail="",
                 doc=None, searched=(), found_agent=None, notes=()):
        self.entry = entry
        self.skill_name = skill_name
        self.status = status
        self.detail = detail
        self.doc = doc
        self.searched = tuple(searched)
        self.found_agent = found_agent
        self.notes = list(notes)

    @property
    def ok(self):
        return self.status in (OK, OK_CROSS_AGENT)


def find_project_root(cwd, env):
    """Locate the Spec Kit project root for this invocation.

    The hook payload's `cwd` is preferred over `CLAUDE_PROJECT_DIR` because it
    follows the session into a git worktree while the environment variable stays
    pinned to wherever the session started.

    The walk stops at a `.git` directory and is depth-limited, so a session running
    outside any project cannot reach up into an unrelated `~/.specify`.
    """
    candidates = [cwd, env.get("CLAUDE_PROJECT_DIR"), os.getcwd()]
    for start in candidates:
        if not start:
            continue
        try:
            current = os.path.realpath(str(start))
        except OSError:
            continue
        for _ in range(MAX_WALK_UP):
            if os.path.isfile(os.path.join(current, ".specify", "extensions.yml")):
                return current
            if os.path.isdir(os.path.join(current, ".specify")):
                return current
            if os.path.exists(os.path.join(current, ".git")):
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def skill_name_for_command(command):
    """`speckit.compound.planverify` -> `speckit-compound-planverify`, or None."""
    if not isinstance(command, str) or not COMMAND_ID_RE.match(command):
        return None
    name = "speckit-" + command[len("speckit."):].replace(".", "-")
    return name if SKILL_NAME_RE.match(name) else None


def extension_id_for_command(command):
    return command.split(".")[1]


def skill_search_paths(root, agent, skill_name):
    """Candidate files, this agent's own layout first."""
    order = [agent] + [other for other in ("claude", "codex") if other != agent]
    return [(other, os.path.join(root, SKILL_DIRS[other], skill_name, "SKILL.md"))
            for other in order]


def resolve(root, agent, entry):
    """Resolve one registration to its installed instruction file."""
    skill_name = skill_name_for_command(entry.command)
    if skill_name is None:
        return ResolvedHook(
            entry, status=INVALID_COMMAND,
            detail="`%s` is not a valid Spec Kit command id; Spec Kit requires "
                   "`speckit.<extension>.<command>` in lowercase" % (entry.command,))

    candidates = skill_search_paths(root, agent, skill_name)
    searched = [path for _, path in candidates]
    errors = []
    for owner, path in candidates:
        try:
            doc = skilldoc.read(path, root)
        except skilldoc.ReadError as error:
            if error.status != skilldoc.NOT_FOUND:
                # A file that exists but cannot be used is a hard answer; falling
                # through to the other agent's copy would hide a real problem.
                return ResolvedHook(entry, skill_name, status=error.status,
                                    detail=error.detail, searched=searched)
            errors.append(error)
            continue

        status = OK if owner == agent else OK_CROSS_AGENT
        notes = list(doc.warnings)
        if owner != agent:
            notes.append(
                "resolved from the %s layout (%s) because no %s copy exists; the "
                "body may contain substitutions made for a different agent"
                % (owner, _relative(path, root), agent))
        notes.extend(crosscheck_manifest(root, entry))
        notes.extend(crosscheck_frontmatter(doc, entry, skill_name))
        if doc.is_empty:
            return ResolvedHook(
                entry, skill_name, status=EMPTY_BODY,
                detail="%s has no instruction body below its frontmatter"
                       % (_relative(path, root),),
                doc=doc, searched=searched, found_agent=owner, notes=notes)
        if doc.looks_contentless:
            notes.append("the body contains only headings or comments; the "
                         "installed command may be a stub")
        return ResolvedHook(entry, skill_name, status=status, doc=doc,
                            searched=searched, found_agent=owner, notes=notes)

    detail = "no SKILL.md at %s" % (_relative(searched[0], root),)
    if len(searched) > 1:
        detail += " (also checked %s)" % (
            ", ".join(_relative(path, root) for path in searched[1:]),)
    legacy = _legacy_hint(root, entry.command, skill_name)
    if legacy:
        detail += "; %s" % legacy
    return ResolvedHook(entry, skill_name, status=NOT_FOUND, detail=detail,
                        searched=searched)


def crosscheck_manifest(root, entry):
    """Compare the registration against the extension's own manifest.

    Advisory only. Spec Kit does not verify that a hook's `command` appears in
    `provides.commands`, and the installed file is what an agent would actually run,
    so a manifest disagreement is worth reporting but must not stop resolution. For
    the same reason a malformed `extension.yml` is a warning here, unlike a
    malformed `extensions.yml`, which blocks.
    """
    notes = []
    extension_id = extension_id_for_command(entry.command)
    if entry.extension and entry.extension != extension_id:
        notes.append(
            "registration declares `extension: %s` but the command id names `%s`; "
            "Spec Kit forces these to match at install time, so this file has "
            "probably been hand-edited" % (entry.extension, extension_id))

    manifest_path = os.path.join(root, ".specify", "extensions", extension_id,
                                 "extension.yml")
    try:
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
    except OSError:
        notes.append("no installed manifest at %s, so the command could not be "
                     "verified against its extension"
                     % (_relative(manifest_path, root),))
        return notes

    try:
        manifest = miniyaml.parse(raw.decode("utf-8"))
    except (miniyaml.YAMLError, UnicodeDecodeError) as error:
        notes.append("%s could not be parsed (%s), so the command could not be "
                     "verified" % (_relative(manifest_path, root), error))
        return notes

    declared = []
    if isinstance(manifest, dict):
        provides = manifest.get("provides")
        if isinstance(provides, dict):
            commands = provides.get("commands")
            if isinstance(commands, list):
                declared = [item for item in commands if isinstance(item, dict)]

    for item in declared:
        aliases = item.get("aliases")
        aliases = aliases if isinstance(aliases, list) else []
        if item.get("name") == entry.command or entry.command in aliases:
            return notes
    notes.append("`%s` is not declared in `provides.commands` of %s"
                 % (entry.command, _relative(manifest_path, root)))
    return notes


def crosscheck_frontmatter(doc, entry, skill_name):
    """Confirm the resolved file is the one the registration meant."""
    notes = []
    name = doc.meta("name")
    if name and name != skill_name:
        notes.append("the resolved file declares `name: %s`, not `%s`; another "
                     "extension may have overwritten this command"
                     % (name, skill_name))
    source = doc.meta("metadata", "source")
    extension_id = extension_id_for_command(entry.command)
    if source and ":" in source and not source.startswith(extension_id + ":"):
        notes.append("the resolved file records `metadata.source: %s`, which does "
                     "not belong to extension `%s`" % (source, extension_id))
    return notes


def _legacy_hint(root, command, skill_name):
    for agent, pattern in LEGACY_HINTS.items():
        for candidate in (command, skill_name):
            path = os.path.join(root, pattern % (candidate,))
            if os.path.exists(path):
                return ("a file exists at the retired location %s, so this project "
                        "was likely installed by an older Spec Kit; re-run "
                        "`specify` to reinstall commands as skills"
                        % (_relative(path, root),))
    return ""


def _relative(path, root):
    try:
        relative = os.path.relpath(str(path), str(root))
    except ValueError:
        return str(path)
    return str(path) if relative.startswith("..") else relative

