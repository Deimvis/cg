"""End-to-end tests for the persistent remote-ref cache driven by
`defaults.cache.{enabled,ttl}`."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cg import config, remote


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: Path) -> Path:
    """Redirect XDG_CACHE_HOME and clear the in-process remote-cache maps so
    each test starts from a clean disk + memory state. Yields the per-test
    cache root (which is `<XDG_CACHE_HOME>/cg/refs/` once anything writes to
    it)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    remote._CACHE_DIR = None
    remote._CACHE_PATH_BY_URL.clear()
    remote._URL_BY_CACHE_PATH.clear()
    return tmp_path / "xdg-cache" / "cg" / "refs"


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    """Replace `_attempt_fetch` with a deterministic stub. Returns the call
    log so tests can assert on call count + URL ordering. The map
    `responses` lets each test queue per-URL response bytes; absent URLs
    default to b"hello\\n"."""
    log: list[tuple[str, str]] = []
    responses: dict[str, bytes] = {}

    def stub(url: str, auth_host: str, source=None):
        log.append((url, auth_host))
        return responses.get(url, b"hello\n"), "application/yaml"

    monkeypatch.setattr(remote, "_attempt_fetch", stub)
    return {"log": log, "responses": responses}


def _enable_cache(run, ttl: dict[str, int] | None = None) -> None:
    """Helper: persist `cache.enabled=true` (and optionally a custom ttl)
    to the test's defaults file so cache_settings() returns the new shape."""
    args = ["config", "defaults", "patch", "cache.enabled=true"]
    if ttl is not None:
        body = ",".join(f"{k}:{v}" for k, v in ttl.items())
        args.append(f"cache.ttl={{{body}}}")
    code, _, err = run(*args)
    assert code == 0, err


def _simulate_new_process() -> None:
    """Drop the in-process cache maps so subsequent fetch() calls have to
    consult the on-disk persistent cache, like a fresh invocation would."""
    remote._CACHE_PATH_BY_URL.clear()
    remote._URL_BY_CACHE_PATH.clear()


URL_A = "https://raw.githubusercontent.com/o/r/main/openapi.yaml"
URL_B = "https://example.invalid/some/other/openapi.yaml"


def test_disabled_by_default_no_persistent_writes(
    cache_root: Path, fake_fetch,
) -> None:
    p1 = remote.fetch(URL_A)
    _simulate_new_process()
    p2 = remote.fetch(URL_A)
    # Two "processes" hit the network twice when caching is off.
    assert len(fake_fetch["log"]) == 2
    # No persistent cache directory was ever created.
    assert not cache_root.exists()
    # Returned paths are still in the per-process scratch dir.
    assert p1.read_bytes() == b"hello\n"
    assert p2.read_bytes() == b"hello\n"


def test_enabled_cache_skips_network_on_second_process(
    cache_root: Path, fake_fetch, run,
) -> None:
    _enable_cache(run)
    p1 = remote.fetch(URL_A)
    _simulate_new_process()
    p2 = remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1  # second fetch served from disk
    assert p2.read_bytes() == b"hello\n"
    assert p2.suffix == ".yaml"  # basename preserved
    # Persistent layout exists.
    assert (cache_root / "blobs").is_dir()
    assert (cache_root / "index").is_dir()
    assert len(list((cache_root / "blobs").iterdir())) == 1
    assert len(list((cache_root / "index").iterdir())) == 1


def test_ttl_expired_triggers_refetch(
    cache_root: Path, fake_fetch, run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_cache(run, ttl={"s": 1})
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    _simulate_new_process()
    # Rewind the index entry's timestamp so it's already expired.
    idx = remote._url_index_path(URL_A)
    entry = json.loads(idx.read_text())
    entry["ts"] = int(time.time()) - 10
    idx.write_text(json.dumps(entry) + "\n")

    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 2  # network was hit again
    # And the index timestamp has been refreshed.
    refreshed = json.loads(idx.read_text())
    assert refreshed["ts"] > entry["ts"]


def test_blob_missing_triggers_refetch(
    cache_root: Path, fake_fetch, run,
) -> None:
    _enable_cache(run)
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    # External GC: blob deleted, index still points at it.
    blobs = list((cache_root / "blobs").iterdir())
    assert len(blobs) == 1
    blobs[0].unlink()
    _simulate_new_process()
    remote.fetch(URL_A)
    # Re-fetched (blob-missing recovery).
    assert len(fake_fetch["log"]) == 2
    # And the blob was restored.
    assert len(list((cache_root / "blobs").iterdir())) == 1


def test_content_sharing_dedupes_blob(
    cache_root: Path, fake_fetch, run,
) -> None:
    """Two distinct canonical URLs that serve identical bytes share one blob
    file and produce two index entries."""
    _enable_cache(run)
    fake_fetch["responses"][URL_A] = b"same-bytes"
    fake_fetch["responses"][URL_B] = b"same-bytes"
    p1 = remote.fetch(URL_A)
    _simulate_new_process()
    p2 = remote.fetch(URL_B)
    assert p1.read_bytes() == p2.read_bytes() == b"same-bytes"
    assert len(list((cache_root / "blobs").iterdir())) == 1
    assert len(list((cache_root / "index").iterdir())) == 2


def test_with_override_enables_cache_transiently(
    cache_root: Path, fake_fetch, run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cg with config defaults patch cache.enabled=true -- ...` must enable
    the cache only for the duration of the inner command — `cache_settings`
    must return `(False, ...)` again afterwards. We capture the override-
    scoped reading by injecting a marker `_dispatch` like the other `with`
    test does."""
    captured: dict = {}
    from cg import cli

    real_dispatch = cli._dispatch

    def fake_dispatch(argv: list[str]) -> int:
        if argv and argv[0] == "noop":
            captured["enabled"], captured["ttl"] = config.cache_settings()
            return 0
        return real_dispatch(argv)

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)

    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--", "noop",
    )
    assert code == 0
    assert captured["enabled"] is True
    # Outside the `with` scope: cache reverts to system default (disabled).
    assert config.cache_settings()[0] is False


def test_returned_path_preserves_yaml_suffix(
    cache_root: Path, fake_fetch, run,
) -> None:
    _enable_cache(run)
    p = remote.fetch("https://raw.githubusercontent.com/o/r/main/spec.yaml")
    assert p.suffix == ".yaml"
    _simulate_new_process()
    p2 = remote.fetch("https://raw.githubusercontent.com/o/r/main/spec.yaml")
    assert p2.suffix == ".yaml"
