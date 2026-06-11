"""End-to-end tests for `cg with <config-mutation> -- <foreign-cmd>...`.

The new grammar: `--` is always a boundary to a foreign command. To exercise
the recursive case we run nested `cg` invocations via `sys.executable -m
cg.cli ...`, which picks up the patched config through `CG_DEFAULTS_CONFIG`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cg import config
from conftest import read_json


SYSTEM = {"cache": {"enabled": False, "ttl": {"d": 7}}}

# Repo root so child `python -m cg.cli` imports the working-tree package,
# not any (possibly stale) system install.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _child_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        _REPO_ROOT + (os.pathsep + existing if existing else ""),
    )


def _cg(*args: str) -> list[str]:
    """Build a command vector for invoking `cg` via the current Python."""
    return [sys.executable, "-m", "cg.cli", *args]


def _seed(run) -> None:
    """Initialize the real defaults file with system defaults."""
    run("config", "defaults", "init")


def test_with_patches_only_for_inner_command(
    run, defaults_file: Path, tmp_path: Path,
) -> None:
    """The nested `cg` invocation sees the patched config via the env var;
    the real file is untouched after the call."""
    _seed(run)
    before = read_json(defaults_file)
    out_file = tmp_path / "out.txt"
    write_script = tmp_path / "write.py"
    write_script.write_text(
        f"import json\n"
        f"from cg import config\n"
        f"with open({str(out_file)!r}, 'w') as f:\n"
        f"    json.dump(list(config.cache_settings()), f)\n"
    )
    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        sys.executable, str(write_script),
    )
    assert code == 0
    assert json.loads(out_file.read_text()) == [True, 604800]
    assert read_json(defaults_file) == before


def test_chained_with_propagates_via_nested_cg(
    run, defaults_file: Path, tmp_path: Path,
) -> None:
    """Two stacked `with` invocations (the inner one runs as a child `cg`)
    propagate the cumulative patch to the innermost foreign command."""
    _seed(run)
    before = read_json(defaults_file)
    out_file = tmp_path / "out.txt"
    write_script = tmp_path / "write.py"
    write_script.write_text(
        f"import json\n"
        f"from cg import config\n"
        f"with open({str(out_file)!r}, 'w') as f:\n"
        f"    json.dump(list(config.cache_settings()), f)\n"
    )
    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        *_cg(
            "with", "config", "defaults", "patch", "cache.ttl={h:1}",
            "--",
            sys.executable, str(write_script),
        ),
    )
    assert code == 0
    # Inner patch's ttl (1h) replaces outer's ttl; outer's `enabled=true`
    # survives because the inner patch is applied on top of (the file the
    # outer wrote, which had enabled=true).
    assert json.loads(out_file.read_text()) == [True, 3600]
    assert read_json(defaults_file) == before


def test_with_missing_tail_errors(run, defaults_file: Path) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, _, err = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
    )
    assert code != 0
    assert "missing '--'" in err
    assert read_json(defaults_file) == before


def test_with_show_action_is_rejected(run, defaults_file: Path) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, _, err = run(
        "with", "config", "defaults",
        "--",
        "true",
    )
    assert code != 0
    assert "missing action" in err
    assert read_json(defaults_file) == before


def test_with_rejects_providers_head(run, defaults_file: Path) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, _, err = run(
        "with", "config", "providers", "add", "github", "github.com", "tok",
        "--",
        "true",
    )
    assert code != 0
    assert "only `config defaults" in err
    assert read_json(defaults_file) == before


def test_with_unknown_command_errors_and_leaves_real_file(
    run, defaults_file: Path,
) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, _, err = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "this-command-does-not-exist-abc123xyz",
    )
    assert code != 0
    assert "command not found" in err
    # Real file untouched even when the foreign command never started.
    assert read_json(defaults_file) == before


def test_with_child_failure_propagates_exit_code(
    run, defaults_file: Path,
) -> None:
    _seed(run)
    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "false",
    )
    assert code != 0


def test_persistent_patch_still_writes_real_file(
    run, defaults_file: Path,
) -> None:
    """Regression guard: `cg config defaults patch` (no `with`) is unchanged."""
    _seed(run)
    code, _, _ = run(
        "config", "defaults", "patch",
        "cache.enabled=true", "cache.ttl={d:14}",
    )
    assert code == 0
    assert read_json(defaults_file) == {
        "cache": {"enabled": True, "ttl": {"d": 14}},
    }


def test_with_init_seeds_transient_view(
    run, defaults_file: Path, tmp_path: Path,
) -> None:
    """`with config defaults init -- <cmd>` makes the inner command see the
    system defaults even if no real file exists."""
    assert not defaults_file.exists()
    out_file = tmp_path / "out.txt"
    write_script = tmp_path / "write.py"
    write_script.write_text(
        f"import json\n"
        f"from cg import config\n"
        f"with open({str(out_file)!r}, 'w') as f:\n"
        f"    json.dump(config.load_defaults(), f)\n"
    )
    code, _, _ = run(
        "with", "config", "defaults", "init",
        "--",
        sys.executable, str(write_script),
    )
    assert code == 0
    assert json.loads(out_file.read_text()) == SYSTEM
    assert not defaults_file.exists()
