"""Strip ANSI/terminal control sequences from PTY output.

Used by ``capture`` and ``wait --text`` so callers can match on the text a
human would see rather than the escape codes a TUI emits.  Best-effort: it
removes the common CSI/OSC/DCS families and two-character escapes, but it is not
a full VT parser.
"""

from __future__ import annotations

import re

_ANSI_RE = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    rb"|\x1b[PX^_][^\x1b]*(?:\x1b\\|\x07)"  # DCS/SOS/PM/APC ... ST/BEL
    rb"|\x1b[@-Z\\-_]"  # two-character escapes
)


def remove_ansi(data: bytes) -> bytes:
    """Return *data* with ANSI control sequences removed."""
    return _ANSI_RE.sub(b"", data)
