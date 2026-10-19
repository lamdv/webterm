"""pi session-file storage helpers.

These routines are pi-specific, but they are ordinary internal helpers: the
core imports them directly. There is no plugin contract here.
"""

from __future__ import annotations

import json
import os

#: Header lines are small; cap the read so a malformed file cannot make the
#: session picker load an arbitrary amount of data.
PI_HEADER_LIMIT = 64 * 1024
#: Maximum entries returned by the session picker in one response.
PI_SESSION_LIST_LIMIT = 500


def prepare_pi_session_dir(path: str) -> str:
    """Create the managed pi session root and return its real path.

    New directories are created ``0700``.  The path is resolved once here so
    every later containment check compares against the same canonical root.
    """
    root = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def read_session_header(path: str) -> dict | None:
    """Read the first ``session`` header line from a pi session file."""
    try:
        with open(path, "rb") as handle:
            line = handle.readline(PI_HEADER_LIMIT)
    except OSError:
        return None
    if not line:
        return None
    try:
        header = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict) or header.get("type") != "session":
        return None
    return header


def resolve_path(root: str | None, raw: object) -> str | None:
    """Resolve *raw* under *root*, or return None.

    This is accident prevention for picker/export paths, not a security
    boundary: pi sessions already have the server user's file access.
    """
    if not root or not isinstance(raw, str) or not raw.strip():
        return None
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    try:
        common = os.path.commonpath([candidate, root])
    except ValueError:
        return None
    return candidate if common == root else None


def list_sessions(
    root: str | None, limit: int = PI_SESSION_LIST_LIMIT
) -> list[dict[str, object]]:
    """List persisted pi sessions under *root*, newest first."""
    if root is None or not os.path.isdir(root):
        return []
    found: list[tuple[float, dict[str, object]]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            if resolve_path(root, path) != os.path.realpath(path):
                continue
            header = read_session_header(path)
            if header is None:
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            found.append(
                (
                    stat.st_mtime,
                    {
                        "path": path,
                        "id": header.get("id"),
                        "cwd": header.get("cwd"),
                        "timestamp": header.get("timestamp"),
                        "parentSession": header.get("parentSession"),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    },
                )
            )
    found.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in found[:limit]]
