"""Ignore-pattern filtering for staged pushes.

Patterns come from push_rules.yaml defaults plus per-repo overrides in
repos.yaml. Semantics (gitignore-flavored, intentionally simple):

- "name/"      directory pattern: excludes any file with a path component
               matching "name" (fnmatch on the component).
- "*.ext"      basename pattern: fnmatch against the file's basename.
- "dir/*.ext"  path pattern: fnmatch against the full relative path.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath


def _matches(path: str, pattern: str) -> bool:
    parts = PurePosixPath(path).parts
    if pattern.endswith("/"):
        component = pattern[:-1]
        return any(fnmatch(part, component) for part in parts)
    if fnmatch(parts[-1], pattern):
        return True
    return fnmatch(path, pattern)


def filter_paths(paths: list[str], patterns: list[str]) -> tuple[list[str], list[str]]:
    """Split paths into (allowed, skipped) according to ignore patterns."""
    allowed: list[str] = []
    skipped: list[str] = []
    for path in paths:
        if any(_matches(path, pattern) for pattern in patterns):
            skipped.append(path)
        else:
            allowed.append(path)
    return allowed, skipped
