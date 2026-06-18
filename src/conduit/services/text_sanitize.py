"""Shared text sanitization: strip ANSI escapes and C0/C1 control bytes.

Provider- and consumer-supplied strings get interpolated into logs and the
line-framed MCP stdio transport, where raw escapes corrupt output. One regex and
one helper, so the call sites (skill_executor, skill_report) can't drift.
"""

import re

# The full CSI escape sequence is matched first so the whole thing is removed (not
# just the ESC byte, which would leave a visible "[31m"), then stray control bytes.
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f\x9b]")


def strip_control_chars(text: str | None) -> str:
    """Remove ANSI escapes and control bytes from ``text`` (None becomes '')."""
    return _CONTROL.sub("", text or "")
