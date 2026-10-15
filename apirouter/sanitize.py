"""Markdown-fence repair for upstream responses.

Our third-party OpenAI-format proxy sometimes wraps its JSON response body in a
markdown code fence, e.g.::

    ```json
    {"id": "chatcmpl-1", ...}
    ```

A strict JSON parser (the client's SDK) chokes on that. ``unwrap_markdown_json``
strips the fence and returns the inner JSON text. It is intentionally
conservative: it only unwraps when the fenced content *looks like* JSON (starts
with ``{`` or ``[``), so it never corrupts a body that legitimately is not
fenced JSON. It is idempotent — applying it to clean JSON returns it unchanged.

This module is pure (no I/O, no third-party deps) so it is trivially unit-tested
independently of the gateway path that consumes it (see gateway.py).
"""

from __future__ import annotations

import re

# A fence that occupies the entire (stripped) string: ```[lang]\n<body>\n```
_FENCE_FULL = re.compile(
    r"^```[ \t]*[a-zA-Z0-9_+-]*[ \t]*\r?\n?(?P<body>.*?)\r?\n?```$",
    re.DOTALL,
)
# A fence appearing anywhere (e.g. after a sentence of prose the proxy prepended).
_FENCE_ANYWHERE = re.compile(
    r"```[ \t]*[a-zA-Z0-9_+-]*[ \t]*\r?\n?(?P<body>.*?)\r?\n?```",
    re.DOTALL,
)


def _looks_like_json(text: str) -> bool:
    s = text.lstrip()
    return s.startswith("{") or s.startswith("[")


def unwrap_markdown_json(text: str) -> str:
    """Return ``text`` with a wrapping markdown code fence removed if (and only
    if) the fenced content looks like JSON. Otherwise return ``text`` unchanged.

    Handles ```` ```json ````, a bare ```` ``` ````, and a fence preceded by
    stray prose. Idempotent on already-clean JSON.
    """
    if not text:
        return text

    stripped = text.strip()

    if stripped.startswith("```"):
        m = _FENCE_FULL.match(stripped)
        if m and _looks_like_json(m.group("body")):
            return m.group("body").strip()
        return text

    # No leading fence — the proxy may have prepended prose before a fenced
    # block ("Here is the response:\n```json ... ```"). Pull the first fenced
    # block out only if it looks like JSON.
    m = _FENCE_ANYWHERE.search(text)
    if m and _looks_like_json(m.group("body")):
        return m.group("body").strip()

    return text
