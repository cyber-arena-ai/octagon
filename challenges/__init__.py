"""Challenge namespace.

The framework keeps only the contract (`challenges.interface`) and discovery
(`challenges.registry`) here. The challenges themselves live in the standalone
selected `challenge-warehouse` checkout, grouped by contributor:

    challenge-warehouse/<contributor>/<challenge>/

To keep every existing `challenges.<challenge>` import working unchanged, each
contributor directory is appended to this package's __path__. Python then finds
`challenges.<challenge>` inside whichever contributor folder holds it — the
contributor layer never appears in a module path, so contributor handles that
aren't valid Python identifiers (e.g. "3rdn4Li") are fine.
"""

from __future__ import annotations

import os
import pathlib

_WAREHOUSE_ENV = "CYBERARENA_CHALLENGE_WAREHOUSE"
_DEFAULT_WAREHOUSE = pathlib.Path(__file__).resolve().parent.parent / "challenge-warehouse"


def _select_warehouse() -> pathlib.Path:
    """Select one warehouse checkout when this package is first imported."""
    configured = os.environ.get(_WAREHOUSE_ENV)
    if configured is None:
        return _DEFAULT_WAREHOUSE

    warehouse = pathlib.Path(configured).expanduser().resolve()
    if not warehouse.is_dir():
        raise RuntimeError(
            f"{_WAREHOUSE_ENV} points to {warehouse}, which is not a directory"
        )
    if (
        not (warehouse / "_registry" / "index.json").is_file()
        or not any(warehouse.glob("*/*/challenge.py"))
    ):
        raise RuntimeError(
            f"{_WAREHOUSE_ENV} points to {warehouse}, which is not a challenge "
            "warehouse checkout (expected _registry/index.json and "
            "<contributor>/<challenge>/challenge.py)"
        )
    return warehouse


_WAREHOUSE = _select_warehouse()
if _WAREHOUSE.is_dir():
    for _contrib in sorted(_WAREHOUSE.iterdir()):
        if _contrib.is_dir() and not _contrib.name.startswith("."):
            _p = str(_contrib)
            if _p not in __path__:
                __path__.append(_p)
