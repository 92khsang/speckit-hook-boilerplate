"""Reading an installed `SKILL.md` and splitting its frontmatter from its body.

The split is purely lexical and happens *before* any YAML parsing. That ordering is
the point: a malformed frontmatter block then costs us only the metadata, never the
instruction body, which is the thing a mandatory gate actually depends on.
"""

import hashlib
import os
import re
import stat

from . import miniyaml

MAX_FILE_BYTES = 256 * 1024

NOT_FOUND = "not_found"
NOT_REGULAR_FILE = "not_regular_file"
TOO_LARGE = "too_large"
UNREADABLE = "unreadable"
PATH_ESCAPE = "path_escape"
NOT_UTF8 = "not_utf8"

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^(?:#{1,6}[ \t].*|[=-]{2,})[ \t]*$", re.MULTILINE)


class ReadError(Exception):
    """A skill file that could not be turned into a usable document."""

    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        Exception.__init__(self, detail)


class SkillDoc(object):
    """An installed skill file, split into metadata and instruction body."""

    __slots__ = ("path", "real_path", "base_dir", "frontmatter", "body", "sha256",
                 "byte_length", "line_count", "warnings")

    def __init__(self, path, real_path, frontmatter, body, sha256, byte_length,
                 warnings):
        self.path = path
        self.real_path = real_path
        self.base_dir = os.path.dirname(path)
        self.frontmatter = frontmatter
        self.body = body
        self.sha256 = sha256
        self.byte_length = byte_length
        self.line_count = body.count("\n") + 1 if body else 0
        self.warnings = warnings

    @property
    def is_empty(self):
        """True only when the body has no non-whitespace content at all.

        This is the sole hard gate. A body that is nothing but headings or comments
        is reported as suspicious but is still delivered: `# Run the linter before
        planning` is a real instruction, and silently discarding it would drop the
        mandatory gate this whole mechanism exists to enforce.
        """
        return self.body.strip() == ""

    @property
    def looks_contentless(self):
        stripped = _HEADING_RE.sub("", _HTML_COMMENT_RE.sub("", self.body))
        return not self.is_empty and stripped.strip() == ""

    def meta(self, *keys):
        """Look up a dotted metadata path, returning "" when absent or not a string."""
        node = self.frontmatter
        for key in keys:
            if not isinstance(node, dict):
                return ""
            node = node.get(key)
        return node if isinstance(node, str) else ""


def read(path, root, max_bytes=MAX_FILE_BYTES):
    """Read and split an installed skill file.

    Symlinks are followed on purpose: Spec Kit's `--dev` install links generated
    commands back into the extension's own cache, and refusing links would break
    that supported layout. The safety check is therefore on the *destination* —
    after opening, the descriptor must be a regular file and its real path must stay
    inside the project. Without that check, a `SKILL.md` symlinked at private
    material would be copied verbatim into the model's context.

    Args:
        path: Path to the candidate `SKILL.md`.
        root: Project root that the resolved file must stay within.
        max_bytes: Refuse anything larger, before reading it into memory.

    Returns:
        SkillDoc: the parsed document.

    Raises:
        ReadError: with a `status` naming which check failed.
    """
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        raise ReadError(NOT_FOUND, "no file at %s" % (path,))
    except IsADirectoryError:
        raise ReadError(NOT_REGULAR_FILE, "%s is a directory" % (path,))
    except OSError as error:
        raise ReadError(UNREADABLE, "cannot open %s: %s" % (path, error))
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise ReadError(NOT_REGULAR_FILE, "%s is not a regular file" % (path,))
        if info.st_size > max_bytes:
            raise ReadError(
                TOO_LARGE,
                "%s is %d bytes, over the %d byte limit" % (path, info.st_size, max_bytes))
        real_path = os.path.realpath(str(path))
        if not _inside(real_path, root):
            raise ReadError(
                PATH_ESCAPE,
                "%s resolves to %s, outside the project root" % (path, real_path))
        try:
            raw = os.read(handle, max_bytes + 1)
        except OSError as error:
            raise ReadError(UNREADABLE, "cannot read %s: %s" % (path, error))
    finally:
        os.close(handle)

    if len(raw) > max_bytes:
        raise ReadError(TOO_LARGE, "%s exceeds the %d byte limit" % (path, max_bytes))
    return _build(str(path), real_path, raw)


def _inside(candidate, root):
    root = os.path.realpath(str(root))
    return candidate == root or candidate.startswith(root + os.sep)


def _build(path, real_path, raw):
    byte_length = len(raw)
    digest = hashlib.sha256(raw).hexdigest()
    warnings = []

    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReadError(NOT_UTF8, "%s is not valid UTF-8: %s" % (path, error))

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    frontmatter_text, body, split_warnings = split_frontmatter(text)
    warnings.extend(split_warnings)

    frontmatter = {}
    if frontmatter_text is not None:
        try:
            parsed = miniyaml.parse(frontmatter_text)
        except miniyaml.YAMLError as error:
            warnings.append("frontmatter could not be parsed (%s); metadata is "
                            "unavailable but the instruction body below is intact"
                            % (error,))
        else:
            if isinstance(parsed, dict):
                frontmatter = parsed
            elif parsed is not None:
                warnings.append("frontmatter is not a mapping; metadata is unavailable")

    return SkillDoc(path, real_path, frontmatter, body, digest, byte_length, warnings)


def split_frontmatter(text):
    """Split a `---` fenced YAML frontmatter block from the instruction body.

    Only an opening `---` on the very first line, at column one, counts; Spec Kit's
    emitter never indents it, and accepting an indented fence would let a document
    that merely starts with a horizontal rule lose its first section.

    An unterminated fence is treated as *no frontmatter at all*, so the whole file
    becomes the body. The alternative — treating everything as metadata — would
    silently delete a mandatory instruction, which is the worst available outcome.

    Returns:
        tuple: `(frontmatter_text_or_None, body, warnings)`.
    """
    warnings = []
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, _trim_body(text), warnings

    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1:])
            return frontmatter, _trim_body(body), warnings

    warnings.append("frontmatter opens with `---` but is never closed; the whole "
                    "file is treated as instruction body")
    return None, _trim_body(text), warnings


def _trim_body(body):
    """Drop leading blank lines and trailing whitespace, and nothing else.

    No heading or comment stripping: the body is delivered to the model verbatim and
    its sha256 is published alongside it, so any further normalization would make
    that hash unverifiable against the file on disk.
    """
    return body.lstrip("\n").rstrip()
