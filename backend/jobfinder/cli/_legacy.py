"""The flat command names from before `jobfinder <group> <verb>`, kept for one release.

`python -m jobfinder.cli add-user --email x` is rewritten to `users add --email x` before Typer parses
it, with a one-line note on stderr. Only `python -m` does this (the old CLI was only ever run that way),
so a bare `jobfinder ingest` shows help instead of starting a full run. The flags are unchanged, except
that `digest send --preview` now needs `--email` (the old `daily --preview` ignored it).
"""
import sys

OLD_NAMES = {
    "ai-check": ["ai", "check"],
    "ingest": ["ingest", "run"],
    "daily": ["digest", "send"],
    "questions": ["questions", "collect"],
    "test-email": ["email", "test"],
    "add-user": ["users", "add"],
    "list-users": ["users", "list"],
    "reset-link": ["users", "reset-link"],
    "grant-admin": ["users", "admin", "grant"],
    "revoke-admin": ["users", "admin", "revoke"],
    "add-board": ["boards", "add"],
    "list-boards": ["boards", "list"],
    "set-board": ["boards", "set"],
}


def translate(argv: list[str]) -> list[str]:
    """argv with an old command name (the first argument) replaced by its group and verb."""
    if not argv:
        return argv
    new = OLD_NAMES.get(argv[0])
    # `ingest` and `questions` are group names too: only a bare one or one followed by flags (other
    # than --help) is the old command.
    if new is None or (argv[0] in ("ingest", "questions") and len(argv) > 1
                       and (not argv[1].startswith("-") or argv[1] == "--help")):
        return argv
    print(f"note: `{argv[0]}` is deprecated and goes away in the next release; "
          f"use `jobfinder {' '.join(new)}`", file=sys.stderr)
    return [*new, *argv[1:]]
