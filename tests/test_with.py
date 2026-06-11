"""End-to-end tests for `cg with <config-mutation> -- <next>`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cg import config
from conftest import read_json


SYSTEM = {"cache": {"enabled": False, "ttl": {"d": 7}}}


def _seed(run) -> None:
    """Initialize the real defaults file with system defaults."""
    run("config", "defaults", "init")


def test_with_patches_only_inner_call(run, defaults_file: Path) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, out, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "config", "defaults",
    )
    assert code == 0
    # Inner show printed enabled:true via the temp file.
    body = out.split("\n", 1)[1]
    assert json.loads(body)["cache"]["enabled"] is True
    # Real file untouched.
    assert read_json(defaults_file) == before
    # And the override was cleared on exit.
    assert config.defaults_path() == defaults_file


def test_chained_with_stacks_patches(run, defaults_file: Path) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, out, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "with", "config", "defaults", "patch", "cache.ttl={h:1}",
        "--",
        "config", "defaults",
    )
    assert code == 0
    body = json.loads(out.split("\n", 1)[1])
    # Inner sees both mutations applied (system defaults overlay adds d:7 in
    # the show view, since cmd_defaults_show prints the effective view).
    assert body["cache"]["enabled"] is True
    assert body["cache"]["ttl"]["h"] == 1
    # Real file untouched.
    assert read_json(defaults_file) == before


def test_with_chained_inner_temp_file_has_replaced_ttl(
    run, defaults_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside two stacked `with` segments, the innermost temp file (what
    `load_defaults` sees) holds exactly the cumulative on-disk shape — the
    inner ttl object replaces the outer one, while the outer `enabled=true`
    survives because the inner patch doesn't touch it."""
    _seed(run)

    captured: dict = {}

    def fake_dispatch(argv: list[str]) -> int:
        # Innermost _run_with installs its override and then calls _dispatch
        # with the tail. We intercept the second nested call (when argv has
        # been peeled past both `with` segments) and snapshot what
        # load_defaults returns under the active override stack.
        if argv and argv[0] == "config":
            captured["data"] = config.load_defaults()
            captured["path"] = config.defaults_path()
            return 0
        return real_dispatch(argv)

    from cg import cli

    real_dispatch = cli._dispatch
    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)

    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "with", "config", "defaults", "patch", "cache.ttl={h:1}",
        "--",
        "config", "defaults",
    )
    assert code == 0
    assert captured["data"] == {"cache": {"enabled": True, "ttl": {"h": 1}}}
    # Inner path is the innermost temp file, not the outer one or the real one.
    assert "cg-with-" in str(captured["path"])
    assert captured["path"] != defaults_file


def test_with_passes_remaining_argv_through(
    run, defaults_file: Path,
) -> None:
    """`-- --help` should hit the openapi/sql parser and print usage."""
    _seed(run)
    code, out, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "--help",
    )
    assert code == 0
    assert "usage: cg" in out
    assert "--src-type" in out


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
        "config", "defaults",
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
        "config", "defaults",
    )
    assert code != 0
    assert "only `config defaults" in err
    assert read_json(defaults_file) == before


def test_with_inner_crash_restores_override_and_real_file(
    run, defaults_file: Path,
) -> None:
    _seed(run)
    before = read_json(defaults_file)
    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "config", "defaults", "nope",  # argparse error → SystemExit(2)
    )
    assert code != 0
    # `finally` restored the override so subsequent code sees the real path.
    assert config.defaults_path() == defaults_file
    assert read_json(defaults_file) == before


def test_persistent_patch_still_writes_real_file(
    run, defaults_file: Path,
) -> None:
    """Regression guard: introducing `with` must not affect plain
    `cg config defaults patch`."""
    _seed(run)
    code, _, _ = run(
        "config", "defaults", "patch",
        "cache.enabled=true", "cache.ttl={d:14}",
    )
    assert code == 0
    assert read_json(defaults_file) == {
        "cache": {"enabled": True, "ttl": {"d": 14}},
    }


def test_with_init_seeds_transient_view(run, defaults_file: Path) -> None:
    """`with config defaults init -- ...` should make the inner call see the
    system defaults even if no real file exists."""
    assert not defaults_file.exists()
    code, out, _ = run(
        "with", "config", "defaults", "init",
        "--",
        "config", "defaults",
    )
    assert code == 0
    body = json.loads(out.split("\n", 1)[1])
    assert body == SYSTEM
    # Real file still not created.
    assert not defaults_file.exists()


def test_with_inner_view_path_points_at_temp_file(
    run, defaults_file: Path,
) -> None:
    _seed(run)
    code, out, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--",
        "config", "defaults",
    )
    assert code == 0
    # First line of `cmd_defaults_show` is `# <path> (effective view, ...)`.
    first = out.splitlines()[0]
    assert first.startswith("# ")
    assert "cg-with-" in first  # the tempfile prefix used by _run_with
    assert str(defaults_file) not in first
