"""Deterministic Spec Kit before-hook injection for Claude Code and Codex CLI.

Reads `.specify/extensions.yml`, resolves `hooks.before_<stage>` command IDs to the
installed skill files, and returns their real instruction bodies to the current
session as `additionalContext`. Never executes hook content.
"""

VERSION = "0.1.0"
