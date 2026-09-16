"""A strict parser for the block-YAML subset Spec Kit emits.

Spec Kit writes `.specify/extensions.yml` with
`yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)`,
which produces a narrow and predictable shape: block mappings, block sequences of
mappings, and scalars — including the two wrapped forms PyYAML's emitter produces at
its default 80-column width (multi-line quoted scalars and folded plain scalars) and
the `{}` / `[]` it uses for empty collections.

This parser targets exactly that shape and **refuses everything else**. Non-empty
flow collections, anchors, aliases, tags, multiple documents, merge keys, complex
keys, tab indentation and duplicate keys all raise `YAMLError`. That refusal is what
makes a dependency-free parser safe here: an unsupported construct becomes a
visible, blocking error instead of a silent misreading. PyYAML, by contrast,
silently accepts duplicate keys and keeps the last value.

Deliberate divergences from PyYAML, documented rather than hidden:

* Only decimal integers are resolved as ints. Hex, octal, underscored and
  sexagesimal forms stay strings. Spec Kit never emits them, and reading one as text
  cannot turn a disabled hook into an enabled one.
* Floats are not resolved. No field in the schema is a float.
* A line that is exactly `---` inside a multi-line quoted scalar would be read as a
  document separator. PyYAML's emitter cannot produce that from this schema.
"""

import re

__all__ = ["YAMLError", "UnsupportedYAML", "parse"]


class YAMLError(Exception):
    """A document this parser will not accept. Always fatal to the caller."""

    def __init__(self, message, line=None, column=None):
        self.message = message
        self.line = line
        self.column = column
        if line is not None:
            message = "line %d:%d: %s" % (line, (column or 0) + 1, message)
        Exception.__init__(self, message)


class UnsupportedYAML(YAMLError):
    """Valid YAML that this subset parser deliberately does not implement."""


_BOOL_TRUE = {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
_BOOL_FALSE = {"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}
_NULL = {"", "~", "null", "Null", "NULL"}
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_EMPTY_MAP_RE = re.compile(r"^\{\s*\}$")
_EMPTY_SEQ_RE = re.compile(r"^\[\s*\]$")
_INDENT_RE = re.compile(r"^[ \t]*")

_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " ",
}


class _Line(object):
    __slots__ = ("indent", "text", "lineno")

    def __init__(self, indent, text, lineno):
        self.indent = indent
        self.text = text
        self.lineno = lineno

    @property
    def is_seq_item(self):
        return self.text == "-" or self.text.startswith("- ")


def _find_key_colon(text):
    """Index of the `:` that terminates a plain key, or -1.

    A colon only ends a key when followed by a space or end of line, which keeps
    `command: speckit.a.b` and `prompt: Execute speckit.a.b?` unambiguous.
    """
    index = 0
    while True:
        index = text.find(":", index)
        if index < 0:
            return -1
        if index + 1 == len(text) or text[index + 1] == " ":
            return index
        index += 1


def _quote_end(text, quote, start):
    """Index of the closing quote at or after `start`, or -1 if the line ends first."""
    index = start
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                return index
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        index += 1
    return -1


def _trailing_backslashes(text):
    count = 0
    while count < len(text) and text[len(text) - 1 - count] == "\\":
        count += 1
    return count


def _fold(pieces, escaped=False):
    """Join wrapped scalar lines the way YAML folds them.

    One line break becomes a space; N consecutive breaks become N-1 newlines. That
    is how PyYAML round-trips an embedded `\\n` through a wrapped quoted scalar.

    With `escaped=True` (double-quoted scalars) a line ending in an odd number of
    backslashes is an escaped line break: the backslash is dropped and the next line
    is joined with no space at all. PyYAML uses that form whenever a wrap point
    would otherwise invent or swallow a space.
    """
    out = ""
    started = False
    breaks = 0
    for piece in pieces:
        if piece == "":
            breaks += 1
            continue
        if not started:
            out = piece
            started = True
        elif escaped and breaks == 0 and _trailing_backslashes(out) % 2 == 1:
            out = out[:-1] + piece
        else:
            out += ("\n" * breaks) if breaks else " "
            out += piece
        breaks = 0
    return out


def _scan(source):
    lines = []
    for number, raw in enumerate(source.split("\n"), start=1):
        leading = _INDENT_RE.match(raw).group(0)
        if "\t" in leading:
            raise YAMLError("tab character in indentation", number, leading.index("\t"))
        lines.append(_Line(len(leading), raw[len(leading):].rstrip(), number))
    return lines


def _is_skippable(line):
    return line.text == "" or line.text.startswith("#")


class _Parser(object):
    def __init__(self, source):
        self.lines = []
        self.pos = 0
        self._filter_documents(_scan(source))

    def _filter_documents(self, scanned):
        """Allow one optional leading `---`; refuse a second document."""
        seen_start = False
        for line in scanned:
            # A document marker is only a marker at column 1; an indented `---` is
            # a continuation line of a wrapped scalar.
            if line.indent == 0 and (line.text == "---" or line.text.startswith("--- ")):
                if seen_start or self.lines:
                    raise UnsupportedYAML(
                        "multiple YAML documents are not supported",
                        line.lineno, line.indent)
                seen_start = True
                continue
            if line.indent == 0 and line.text == "...":
                break
            self.lines.append(line)

    # -- line stream -------------------------------------------------------

    def _peek(self):
        while self.pos < len(self.lines) and _is_skippable(self.lines[self.pos]):
            self.pos += 1
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def _next(self):
        line = self._peek()
        if line is not None:
            self.pos += 1
        return line

    def _raw(self):
        """The next line without skipping blanks or comments.

        Continuation lines of a wrapped scalar are content, so they must never go
        through the comment filter.
        """
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def _push(self, line):
        self.lines.insert(self.pos, line)

    # -- structure ---------------------------------------------------------

    def parse(self):
        line = self._peek()
        if line is None:
            return None
        if line.indent != 0:
            raise YAMLError("document must start at column 1", line.lineno, line.indent)
        value = self._parse_block(0)
        trailing = self._peek()
        if trailing is not None:
            raise YAMLError("unexpected content", trailing.lineno, trailing.indent)
        return value

    def _parse_block(self, indent):
        line = self._peek()
        if line is None or line.indent != indent:
            return None
        if line.is_seq_item:
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_sequence(self, indent):
        items = []
        while True:
            line = self._peek()
            if line is None or line.indent != indent or not line.is_seq_item:
                break
            self._next()
            content = line.text[1:]
            if content.strip() == "":
                nested = self._peek()
                if nested is not None and nested.indent > indent:
                    items.append(self._parse_block(nested.indent))
                else:
                    items.append(None)
                continue
            offset = len(content) - len(content.lstrip(" "))
            inner = content.strip()
            column = indent + 1 + offset
            if not self._starts_block(inner, line):
                items.append(self._read_scalar_value(inner, line, indent))
                continue
            # The payload starts a node whose indentation is its own column.
            # Re-inject it as a synthetic line so a mapping begun on the `-` line
            # picks up its continuation lines (`- a: 1` / `  b: 2`) uniformly.
            self._push(_Line(column, inner, line.lineno))
            items.append(self._parse_block(column))
        return items

    def _starts_block(self, text, line):
        """Does this `- ...` payload begin a nested mapping or sequence?

        `- adrkit` is a scalar item; `- extension: compound` opens a mapping whose
        remaining keys arrive on later lines. Telling them apart is a matter of
        finding a key-terminating `:` outside of quotes.
        """
        if text == "-" or text.startswith("- "):
            return True
        if text[:1] in ("'", '"'):
            end = _quote_end(text, text[0], 1)
            return end >= 0 and text[end + 1:].startswith(":")
        return _find_key_colon(text) >= 0

    def _parse_mapping(self, indent):
        result = {}
        while True:
            line = self._peek()
            if line is None or line.indent != indent or line.is_seq_item:
                break
            self._next()
            key, rest = self._split_key(line)
            if key in result:
                raise YAMLError("duplicate key %r" % (key,), line.lineno, line.indent)
            result[key] = self._parse_value(indent, rest, line)
        deeper = self._peek()
        if deeper is not None and deeper.indent > indent:
            raise YAMLError("unexpected indentation", deeper.lineno, deeper.indent)
        return result

    def _split_key(self, line):
        text = line.text
        if text.startswith("? "):
            raise UnsupportedYAML("explicit complex keys are not supported",
                                  line.lineno, line.indent)
        if text.startswith("<<:"):
            raise UnsupportedYAML("merge keys are not supported", line.lineno, line.indent)
        if text[0] in "'\"":
            end = _quote_end(text, text[0], 1)
            if end < 0:
                raise YAMLError("unterminated quoted key", line.lineno, line.indent)
            key = self._unquote(text[:end + 1], line)
            remainder = text[end + 1:]
            if not remainder.startswith(":"):
                raise YAMLError("expected ':' after quoted key", line.lineno, line.indent)
            return key, remainder[1:].strip()
        index = _find_key_colon(text)
        if index < 0:
            raise YAMLError("expected 'key: value'", line.lineno, line.indent)
        key = text[:index].strip()
        if not key:
            raise YAMLError("empty key", line.lineno, line.indent)
        if key[0] in "&*!":
            raise UnsupportedYAML("anchors, aliases and tags are not supported",
                                  line.lineno, line.indent)
        return self._resolve_plain(key, line), text[index + 1:].strip()

    def _parse_value(self, indent, rest, line):
        if rest.startswith("#"):
            rest = ""
        if rest and rest[0] in "|>":
            return self._read_block_scalar(indent, rest, line)
        if rest:
            return self._read_scalar_value(rest, line, indent)
        nested = self._peek()
        if nested is None:
            return None
        if nested.indent > indent:
            return self._parse_block(nested.indent)
        # PyYAML emits `key:` followed by a sequence at the *same* column as the key.
        if nested.indent == indent and nested.is_seq_item:
            return self._parse_sequence(indent)
        return None

    # -- scalars -----------------------------------------------------------

    def _read_scalar_value(self, text, line, owner_indent):
        """Read a scalar that may continue onto more-indented following lines."""
        if text[0] in "'\"":
            return self._read_quoted_value(text, line)
        pieces = [text]
        wrapped = False
        while True:
            candidate = self._raw()
            if candidate is None:
                break
            if candidate.text != "" and candidate.indent <= owner_indent:
                break
            # A more-indented line after a scalar value is a wrap of that scalar, so
            # a leading `- ` there is content rather than a sequence entry. A
            # key-terminating colon is different: YAML forbids a mapping entry in
            # this position, and PyYAML's emitter quotes any scalar that would wrap
            # into one, so seeing it means the file is mis-indented.
            if candidate.text != "" and _find_key_colon(candidate.text) >= 0:
                raise YAMLError("unexpected indentation",
                                candidate.lineno, candidate.indent)
            pieces.append(candidate.text)
            wrapped = wrapped or candidate.text != ""
            self.pos += 1
        while len(pieces) > 1 and pieces[-1] == "":
            pieces.pop()
            self.pos -= 1
        if not wrapped:
            return self._resolve_plain(text, line)
        # A wrapped plain scalar is always a string; type resolution would be wrong.
        return _fold(pieces)

    def _read_quoted_value(self, text, line):
        quote = text[0]
        end = _quote_end(text, quote, 1)
        if end >= 0:
            trailing = text[end + 1:].strip()
            if trailing and not trailing.startswith("#"):
                raise YAMLError("unexpected text after quoted scalar",
                                line.lineno, line.indent)
            return self._unquote(text[:end + 1], line)
        pieces = [text[1:]]
        while True:
            candidate = self._raw()
            if candidate is None:
                raise YAMLError("unterminated quoted scalar", line.lineno, line.indent)
            self.pos += 1
            stop = _quote_end(candidate.text, quote, 0)
            if stop >= 0:
                pieces.append(candidate.text[:stop])
                trailing = candidate.text[stop + 1:].strip()
                if trailing and not trailing.startswith("#"):
                    raise YAMLError("unexpected text after quoted scalar",
                                    candidate.lineno, candidate.indent)
                break
            pieces.append(candidate.text)
        return self._unquote(quote + _fold(pieces, escaped=(quote == '"')) + quote, line)

    def _unquote(self, token, line):
        quote = token[0]
        body = token[1:-1]
        out = []
        index = 0
        while index < len(body):
            char = body[index]
            if quote == "'":
                if char == "'" and index + 1 < len(body) and body[index + 1] == "'":
                    out.append("'")
                    index += 2
                    continue
                out.append(char)
                index += 1
                continue
            if char == "\\":
                index += 1
                if index >= len(body):
                    raise YAMLError("truncated escape sequence", line.lineno, line.indent)
                code = body[index]
                if code in ("x", "u", "U"):
                    width = {"x": 2, "u": 4, "U": 8}[code]
                    digits = body[index + 1:index + 1 + width]
                    if len(digits) != width:
                        raise YAMLError("truncated escape sequence",
                                        line.lineno, line.indent)
                    try:
                        out.append(chr(int(digits, 16)))
                    except ValueError:
                        raise YAMLError("invalid escape sequence",
                                        line.lineno, line.indent)
                    index += 1 + width
                    continue
                if code == "\n":
                    index += 1
                    continue
                if code not in _ESCAPES:
                    raise YAMLError("unknown escape %r" % ("\\" + code,),
                                    line.lineno, line.indent)
                out.append(_ESCAPES[code])
                index += 1
                continue
            out.append(char)
            index += 1
        return "".join(out)

    def _read_block_scalar(self, indent, header, line):
        style = header[0]
        chomp = ""
        for char in header[1:]:
            if char in "-+":
                chomp = char
            elif char.isdigit():
                raise UnsupportedYAML(
                    "explicit block scalar indentation is not supported",
                    line.lineno, line.indent)
            elif char in ("#", " "):
                break
            else:
                raise YAMLError("invalid block scalar header %r" % (header,),
                                line.lineno, line.indent)
        body = []
        block_indent = None
        while self.pos < len(self.lines):
            candidate = self.lines[self.pos]
            if candidate.text == "":
                body.append("")
                self.pos += 1
                continue
            if candidate.indent <= indent:
                break
            if block_indent is None:
                block_indent = candidate.indent
            body.append(" " * max(0, candidate.indent - block_indent) + candidate.text)
            self.pos += 1
        while body and body[-1] == "":
            body.pop()
        if style == "|":
            text = "\n".join(body)
        else:
            text = _fold(body)
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n"
        return text + "\n" if text else text

    def _resolve_plain(self, text, line):
        if text == "":
            return None
        first = text[0]
        if first in "[{":
            if _EMPTY_MAP_RE.match(text):
                return {}
            if _EMPTY_SEQ_RE.match(text):
                return []
            raise UnsupportedYAML("flow collections are not supported",
                                  line.lineno, line.indent)
        if first in "&*":
            raise UnsupportedYAML("anchors and aliases are not supported",
                                  line.lineno, line.indent)
        if first == "!":
            raise UnsupportedYAML("explicit tags are not supported",
                                  line.lineno, line.indent)
        # In a plain scalar, ` #` starts a comment; a `#` inside a word does not.
        comment = re.search(r"(?:^|\s)#", text)
        if comment is not None:
            text = text[:comment.start()].rstrip()
        if text in _NULL:
            return None
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        if _INT_RE.match(text):
            return int(text, 10)
        return text


def parse(source):
    """Parse a YAML document. Raises YAMLError for anything outside the subset."""
    if not isinstance(source, str):
        raise YAMLError("source must be text")
    return _Parser(source).parse()
