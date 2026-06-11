"""End-to-end tests for the `CG_DEFAULTS_CONFIG` env-var contract that
`cg with ... -- <foreign-cmd>` uses to propagate the patched config to
nested `cg` invocations under the foreign command."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from cg import config
from conftest import read_json


SYSTEM = {"cache": {"enabled": False, "ttl": {"d": 7}}}

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _child_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prepend repo root to PYTHONPATH so subprocess `python3` runs find the
    working-tree cg package, not the (possibly stale) system-installed one."""
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        _REPO_ROOT + (os.pathsep + existing if existing else ""),
    )


def test_env_var_redirects_defaults_path(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CG_DEFAULTS_CONFIG is set, `defaults_path()` points at it,
    overriding the XDG location."""
    custom = tmp_path / "elsewhere" / "defaults.json"
    custom.parent.mkdir(parents=True)
    custom.write_text(json.dumps({"cache": {"enabled": True, "ttl": {"h": 2}}}))
    monkeypatch.setenv(config.DEFAULTS_CONFIG_ENV, str(custom))

    assert config.defaults_path() == custom
    assert config.cache_settings() == (True, 7200)


def test_with_propagates_patched_config_to_child_via_env(
    xdg: Path, tmp_path: Path, run, defaults_file: Path,
) -> None:
    """`cg with config defaults patch ... -- <foreign-cmd>` makes the
    foreign command (and anything it spawns, including nested `cg`)
    read the patched config through `CG_DEFAULTS_CONFIG`. This is the
    `make ci → nested cg --src-type=...` scenario."""
    run("config", "defaults", "init")
    before = read_json(defaults_file)

    out_file = tmp_path / "child-out.txt"
    child_script = tmp_path / "child.py"
    child_script.write_text(textwrap.dedent(f"""
        import json, os
        from cg import config
        with open({str(out_file)!r}, "w") as f:
            json.dump({{
                "env": os.environ.get(config.DEFAULTS_CONFIG_ENV),
                "settings": list(config.cache_settings()),
                "path": str(config.defaults_path()),
            }}, f)
    """))

    code, _, _ = run(
        "with", "config", "defaults", "patch",
        "cache.enabled=true", "cache.ttl={h:2}",
        "--", sys.executable, str(child_script),
    )
    assert code == 0

    seen = json.loads(out_file.read_text())
    assert seen["env"] is not None
    assert seen["env"].endswith("/defaults.json")
    assert seen["settings"] == [True, 7200]
    assert read_json(defaults_file) == before
