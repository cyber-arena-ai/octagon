"""Import-time selection of the challenge warehouse checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_NAME = "CYBERARENA_CHALLENGE_WAREHOUSE"


def _warehouse(tmp_path: Path, name: str = "warehouse") -> Path:
    root = tmp_path / name
    package = root / "contributor" / "external_probe"
    package.mkdir(parents=True)
    (root / "_registry").mkdir()
    (root / "_registry" / "index.json").write_text("{}")
    (package / "__init__.py").write_text("SOURCE = 'external'\n")
    (package / "challenge.py").write_text("# warehouse layout marker\n")
    return root


def _run(code: str, warehouse: Path | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if warehouse is None:
        env.pop(ENV_NAME, None)
    else:
        env[ENV_NAME] = str(warehouse)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_default_warehouse_remains_bundled_submodule():
    proc = _run("import challenges; print(challenges._WAREHOUSE)", None)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == REPO_ROOT / "challenge-warehouse"


def test_external_warehouse_is_the_only_contributor_source(tmp_path):
    warehouse = _warehouse(tmp_path)
    code = """
import json
import challenges
from challenges.external_probe import SOURCE
print(json.dumps({
    "warehouse": str(challenges._WAREHOUSE),
    "paths": list(challenges.__path__),
    "source": SOURCE,
}))
"""
    proc = _run(code, warehouse)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert Path(result["warehouse"]) == warehouse.resolve()
    assert result["source"] == "external"
    assert str(warehouse / "contributor") in result["paths"]
    assert not any(
        str(REPO_ROOT / "challenge-warehouse") in path
        for path in result["paths"]
    )


def test_explicit_invalid_warehouse_fails_clearly(tmp_path):
    invalid = tmp_path / "not-a-warehouse"
    invalid.mkdir()
    proc = _run("import challenges", invalid)
    assert proc.returncode != 0
    assert ENV_NAME in proc.stderr
    assert "not a challenge warehouse checkout" in proc.stderr


def test_warehouse_selection_does_not_hot_swap(tmp_path):
    first = _warehouse(tmp_path, "first")
    second = _warehouse(tmp_path, "second")
    code = f"""
import os
import challenges
before = challenges._WAREHOUSE
os.environ[{ENV_NAME!r}] = {str(second)!r}
print(before == challenges._WAREHOUSE)
"""
    proc = _run(code, first)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True"
