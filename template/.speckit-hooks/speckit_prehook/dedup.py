"""Suppressing a repeat injection inside a single user turn.

The key is `(agent, session_id, turn_key, registry_event)`. Claude's `prompt_id` is
documented as the UUID correlating one user prompt with every event until the next
prompt, and it is present on both Claude routes; Codex's input schema makes
`turn_id` required. That is exactly one logical turn, so a genuine re-run of the
same stage later in the session arrives with a new key and injects again — no TTL
guesswork is involved in the correctness of the decision.

Every failure mode here is fail-open. A bookkeeping problem must never be able to
suppress a hook.
"""

import errno
import hashlib
import json
import os
import time

STATE_ENV = "SPECKIT_PREHOOK_STATE_DIR"
ENTRY_TTL_SECONDS = 6 * 60 * 60
MAX_CLEANUP_UNLINKS = 200


class Claim(object):
    """The right to emit for one key, or a no-op when dedup is unavailable."""

    __slots__ = ("path", "granted")

    def __init__(self, path=None, granted=True):
        self.path = path
        self.granted = granted

    def finalize(self, info):
        """Record what was emitted. Purely diagnostic; never read back for decisions."""
        if not self.path:
            return
        try:
            with open(self.path, "w") as handle:
                json.dump(info, handle, ensure_ascii=False, sort_keys=True)
        except OSError:
            pass

    def release(self):
        """Drop an unused claim so a later invocation is not wrongly suppressed."""
        if not self.path:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass


def state_dir(root, env=None):
    env = os.environ if env is None else env
    base = env.get(STATE_ENV)
    if not base:
        base = env.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state")
        base = os.path.join(base, "speckit-prehook")
    project = hashlib.sha256(os.path.realpath(str(root)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, project)


def key_digest(agent, session_id, turn_key, event):
    material = "\0".join([agent or "", session_id or "", turn_key or "", event or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def claim(root, route, env=None):
    """Try to become the one invocation that injects for this turn.

    Returns a granted `Claim` when this invocation should emit, and a refused one
    when an earlier invocation in the same turn already did. Without a session id or
    a turn key there is nothing to correlate on, so dedup is disabled rather than
    guessed at.
    """
    if not route.session_id or not route.turn_key:
        return Claim(granted=True)

    directory = state_dir(root, env)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        return Claim(granted=True)

    path = os.path.join(directory, key_digest(
        route.agent, route.session_id, route.turn_key, route.registry_event))
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as error:
        if error.errno != errno.EEXIST:
            return Claim(granted=True)
        # A zero-length file is an abandoned claim from a process that died before
        # emitting. Re-claiming risks a rare duplicate injection; refusing would risk
        # losing the injection entirely, which is the worse trade.
        try:
            if os.path.getsize(path) == 0:
                return Claim(path, granted=True)
        except OSError:
            return Claim(granted=True)
        return Claim(path, granted=False)
    os.close(handle)
    _cleanup(directory)
    return Claim(path, granted=True)


def _cleanup(directory):
    """Opportunistically drop expired entries, bounded so the hot path stays fast."""
    cutoff = time.time() - ENTRY_TTL_SECONDS
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if removed >= MAX_CLEANUP_UNLINKS:
            return
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
