"""Load a .env file, so credentials do not have to be exported every run.

Deliberately not a dependency: this reads the subset of the format that
matters -- KEY=value, optional quotes, # comments -- and stops.

Existing environment variables win. A value exported in the shell is an
explicit instruction for this run; a .env file is a stored default, and a
stored default must not override what the caller just said.
"""

from __future__ import annotations

import os
from pathlib import Path

LOADED: list[str] = []


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        # Strip one layer of matching quotes, so a value containing spaces or
        # a trailing # survives.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def find(start: Path | None = None) -> Path | None:
    """The nearest .env at or above *start*, or None.

    A .env that only loads when the process happens to have been started in
    the directory holding it is a trap. Nothing fails at startup: the key is
    simply absent, and it surfaces much later -- as a render that dies saying
    no model key is configured, while the key is sitting in a file one
    directory up. Running `pycoding-worker` from `web/` after `npm run dev`
    is exactly how that happens.

    The walk stops at the repository root when there is one, so a .env beside
    an unrelated checkout higher up is not picked up by accident.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            break
    return None


def load(path: Path | str | None = None, *, override: bool = False) -> list[str]:
    """Apply the .env to the environment. Returns the names that were set.

    With no *path*, the nearest one at or above the working directory is used,
    so a command run from a subdirectory finds the same file as one run from
    the root. A path given explicitly is used as given.
    """
    file = Path(path) if path is not None else find()
    if file is None or not file.is_file():
        return []

    applied = []
    for key, value in parse(file.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    if applied:
        LOADED.extend(applied)
    return applied
