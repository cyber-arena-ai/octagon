"""Static source/reference helpers — no imports of the challenge's own deps.

`source_binds` parses a .py file instead of importing it, so the static phase
never pulls in build.py's docker SDK. `image_repo`/`image_tag` split an OCI
reference. `accepts_kwarg` introspects a prompt builder's signature.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Callable


def accepts_kwarg(fn: Callable, kw: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # can't introspect (builtins) — don't flag
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return True
    return kw in sig.parameters


def source_binds(path: Path, name: str) -> bool:
    """True if the .py file at `path` binds `name` at module top level — via
    def / async def, assignment, or import. Parses the source; never imports,
    so the static phase stays free of the module's own deps (build.py pulls in
    the docker SDK at import time, which the no-Docker phase must avoid)."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return True
    return False


def image_repo(ref: str) -> str:
    """Repository part of an OCI reference, dropping any :tag and @digest.
    The tag colon is the last ':' that follows the last '/', so a registry
    host:port (e.g. localhost:5000/foo) isn't mistaken for a tag."""
    ref = ref.split("@", 1)[0]
    colon, slash = ref.rfind(":"), ref.rfind("/")
    return ref[:colon] if colon > slash else ref


def image_tag(ref: str) -> str:
    """The :tag of an OCI reference (empty string if none) — inverse of
    image_repo. A registry host:port isn't mistaken for a tag (same rule)."""
    ref = ref.split("@", 1)[0]
    colon, slash = ref.rfind(":"), ref.rfind("/")
    return ref[colon + 1:] if colon > slash else ""
