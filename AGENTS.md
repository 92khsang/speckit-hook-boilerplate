`template/` is the payload this repository ships: `scripts/install.sh` copies it into
*other* repositories. It is not this repository's own agent configuration.

## Rules

- `template/.speckit-hooks/` is Python 3.9+ standard library only. A third-party import
  breaks the claim the whole design rests on; if one looks necessary, stop and ask.
- `template/.speckit-hooks/speckit-hook` stays POSIX `sh`. When `python3` is missing the
  hook must still refuse, and refusing on Codex means printing JSON — which Python
  cannot do when Python is what is missing.
- The two CLIs refuse differently: Claude Code blocks with exit 2, Codex blocks by
  printing `{"decision":"block",...}` with exit **0**. Using one route's mechanism on
  the other makes the gate disappear without any error.
- A mandatory hook that cannot be fully loaded blocks the request. Never truncate a
  mandatory body and never downgrade a mandatory failure to a warning: a half-injected
  gate the model believes it satisfied is worse than a refusal.
- Nothing read from stdin, `extensions.yml`, or a skill file is ever passed to `eval`,
  `bash -c`, or shell interpolation.
- `miniyaml.py` refuses input outside the subset Spec Kit emits, by design. Widening
  what it accepts requires checking the new form against PyYAML first.
- Statements about Claude Code or Codex behaviour in `README.md` and
  `docs/guarantees.md` are measurements. Measure before writing one, and name the CLI
  version you measured.
- `scripts/package.sh` must ship everything `README.md` points at. The archive has
  shipped with dangling references before.

## Commands

```sh
python3 -m unittest discover -s tests -t tests   # full suite; stdlib only, no network
scripts/package.sh                               # build dist/ (gitignored)
scripts/install.sh --target /path/to/repo        # install into another repository
scripts/verify-claude.sh                         # live Claude end-to-end; spends quota
scripts/probe-codex.sh                           # why Codex is not running the hook
```

Editing the `command` string in `template/.codex/hooks.json` changes its hash and
revokes Codex's hook trust, so the hook silently stops firing until it is re-approved
with `/hooks` in the Codex TUI.

## Boundaries

`template/` is self-contained and is copied out on its own: nothing under it may import
from `tests/` or `scripts/`. The dependency runs one way, tests into `template/`.

## Contributing

`main` is protected — pull request required, no direct push, linear history, squash
merge only. Branch as `<type>/<slug>`. The squash commit takes the **PR title**, so PR
titles follow Conventional Commits; `.gitmessage` is the local template. Release by
pushing a `vX.Y.Z` tag, which builds and attaches the archive; never commit `dist/`.
Do not push or open a pull request on my behalf without asking.

## Pointers

- [docs/guarantees.md](docs/guarantees.md) — measured Claude Code and Codex hook
  behaviour, and what the runner does and does not guarantee. Read it before changing
  stage matching, either refusal path, or anything in `adapters.py`.
- [docs/migration.md](docs/migration.md) — migrating a repository that still has the
  earlier generation of this boilerplate installed; read it before removing one by
  hand, because none of its configuration carries over.
