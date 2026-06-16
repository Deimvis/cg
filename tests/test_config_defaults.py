"""End-to-end tests for `cg config defaults` (init / reset / patch / show)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cg import config
from conftest import read_json


SYSTEM = {
    "cache": {
        "enabled": True,
        "ttl": {"d": 7},
        "read": {
            "enabled": True,
            "categories": ["invalidatable"],
            "invalidation_first": True,
        },
        "write": {"enabled": True},
    },
}


def test_show_with_no_file_prints_system_defaults(run, defaults_file: Path) -> None:
    code, out, _ = run("config", "defaults")
    assert code == 0
    assert not defaults_file.exists()
    # the second line onwards is the JSON dump
    body = out.split("\n", 1)[1]
    assert json.loads(body) == SYSTEM


def test_init_creates_file_with_system_defaults(run, defaults_file: Path) -> None:
    code, _, _ = run("config", "defaults", "init")
    assert code == 0
    assert read_json(defaults_file) == SYSTEM


def test_init_is_noop_when_file_exists(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    defaults_file.write_text('{"cache":{"enabled":true,"ttl":{"d":1}}}\n')
    code, out, _ = run("config", "defaults", "init")
    assert code == 0
    assert "already exists" in out
    assert read_json(defaults_file) == {"cache": {"enabled": True, "ttl": {"d": 1}}}


def test_reset_overwrites_existing_file(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    defaults_file.write_text('{"cache":{"enabled":true,"ttl":{"h":3}}}\n')
    code, out, _ = run("config", "defaults", "reset")
    assert code == 0
    assert "overwrote" in out
    assert read_json(defaults_file) == SYSTEM


def test_reset_creates_when_missing(run, defaults_file: Path) -> None:
    code, out, _ = run("config", "defaults", "reset")
    assert code == 0
    assert "created" in out
    assert read_json(defaults_file) == SYSTEM


def test_patch_scalar_and_object_value(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    code, _, _ = run(
        "config", "defaults", "patch",
        "cache.enabled=true", "cache.ttl={d:14}",
    )
    assert code == 0
    on_disk = read_json(defaults_file)
    assert on_disk["cache"]["enabled"] is True
    assert on_disk["cache"]["ttl"] == {"d": 14}


def test_patch_object_value_replaces_existing_dict(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    run("config", "defaults", "patch", "cache.ttl={d:14}")
    code, _, _ = run("config", "defaults", "patch", "cache.ttl={h:6}")
    assert code == 0
    # Whole-object replacement: only h:6, no leftover d:14.
    assert read_json(defaults_file)["cache"]["ttl"] == {"h": 6}


def test_patch_creates_file_when_absent(run, defaults_file: Path) -> None:
    assert not defaults_file.exists()
    code, _, _ = run("config", "defaults", "patch", "cache.enabled=true")
    assert code == 0
    # Patch without a prior file only writes the keys explicitly patched.
    assert read_json(defaults_file) == {"cache": {"enabled": True}}


def test_patch_unknown_top_level_key_is_rejected(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run("config", "defaults", "patch", "cahce.enabled=true")
    assert code != 0
    assert "unknown key" in err.lower() or "unknown key" in err
    assert read_json(defaults_file) == before  # untouched


def test_patch_rejects_invalid_scalar(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run("config", "defaults", "patch", "cache.enabled=nope")
    assert code != 0
    assert "cache.enabled" in err and "boolean" in err
    assert read_json(defaults_file) == before


def test_patch_rejects_zero_total_duration(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run("config", "defaults", "patch", "cache.ttl={d:0}")
    assert code != 0
    assert "total duration" in err
    assert read_json(defaults_file) == before


def test_patch_file_type_errors_cleanly(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run(
        "config", "defaults", "patch", "-t", "patch-file", "foo.patch",
    )
    assert code != 0
    assert "not yet implemented" in err
    assert read_json(defaults_file) == before


def test_patch_multiple_args_applied_left_to_right(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    code, _, _ = run(
        "config", "defaults", "patch",
        "cache.ttl={d:1}", "cache.ttl={h:3}",
    )
    assert code == 0
    # Per-arg replacement: second arg's value wins.
    assert read_json(defaults_file)["cache"]["ttl"] == {"h": 3}


def test_cache_settings_uses_system_defaults_when_no_file(
    run, defaults_file: Path,
) -> None:
    """`cache_settings()` returns the merged view, falling back to system
    defaults for any subtree the user hasn't set."""
    assert not defaults_file.exists()
    s = config.cache_settings()
    assert s.read_enabled is True
    assert s.write_enabled is True
    assert s.read_categories == frozenset({"invalidatable"})
    assert s.invalidation_first is True
    assert s.ttl_seconds == 7 * 86400


def test_legacy_minimal_schema_still_loads(run, defaults_file: Path) -> None:
    """A user file from before `read`/`write` were added is still accepted;
    missing subtrees fall back to system defaults."""
    defaults_file.parent.mkdir(parents=True, exist_ok=True)
    defaults_file.write_text('{"cache":{"enabled":false,"ttl":{"d":1}}}\n')
    s = config.cache_settings()
    # Master switch off → both gates collapse to False.
    assert s.read_enabled is False
    assert s.write_enabled is False
    assert s.ttl_seconds == 86400


def test_patch_rejects_bad_category(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run(
        "config", "defaults", "patch",
        "cache.read={enabled:true,categories:[bogus],invalidation_first:true}",
    )
    assert code != 0
    assert "categories" in err
    assert read_json(defaults_file) == before


def test_patch_rejects_non_bool_read_enabled(run, defaults_file: Path) -> None:
    run("config", "defaults", "init")
    before = read_json(defaults_file)
    code, _, err = run(
        "config", "defaults", "patch",
        "cache.read={enabled:nope,categories:[all],invalidation_first:true}",
    )
    assert code != 0
    assert "cache.read.enabled" in err and "boolean" in err
    assert read_json(defaults_file) == before
