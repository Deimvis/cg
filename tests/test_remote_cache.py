"""End-to-end tests for the persistent remote-ref cache driven by the
`defaults.cache` schema (enabled / read / write / ttl)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cg import config, remote


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: Path) -> Path:
    """Redirect XDG_CACHE_HOME and reset the in-process remote-cache maps so
    each test starts from a clean disk + memory state."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    remote._CACHE_DIR = None
    remote._CACHE_PATH_BY_URL.clear()
    remote._URL_BY_CACHE_PATH.clear()
    return tmp_path / "xdg-cache" / "cg" / "refs"


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    """Replace `_attempt_fetch` with a deterministic stub. Returns a control
    object the test mutates:
      - `log`: list of `(url, auth_host, conditional)` tuples per call,
      - `responses[url]`: response bytes (default `b"hello\\n"`),
      - `headers[url]`: response headers dict (default no ETag/Last-Modified),
      - `not_modified[url]`: bool — if True and a conditional request is
        sent for this URL, the stub returns `_NOT_MODIFIED`.
    """
    log: list[tuple[str, str, dict | None]] = []
    responses: dict[str, bytes] = {}
    headers: dict[str, dict[str, str]] = {}
    not_modified: dict[str, bool] = {}

    def stub(url, auth_host, source=None, conditional=None):
        log.append((url, auth_host, dict(conditional) if conditional else None))
        if conditional and not_modified.get(url):
            return remote._NOT_MODIFIED, "", headers.get(url, {})
        return responses.get(url, b"hello\n"), "application/yaml", headers.get(url, {})

    monkeypatch.setattr(remote, "_attempt_fetch", stub)
    return {
        "log": log,
        "responses": responses,
        "headers": headers,
        "not_modified": not_modified,
    }


def _patch(run, *args: str) -> None:
    """Write a patch through `cg config defaults patch ...` and assert success."""
    code, _, err = run("config", "defaults", "patch", *args)
    assert code == 0, err


def _set_cache(
    run,
    *,
    enabled: bool = True,
    read_enabled: bool = True,
    categories: list[str] | None = None,
    invalidation_first: bool = True,
    write_enabled: bool = True,
    ttl: dict[str, int] | None = None,
) -> None:
    """Write the whole `cache.*` subtree in one go so the test's config is
    explicit, not relying on system defaults. Default `categories` mirrors
    the system default."""
    cats = categories if categories is not None else ["all", "invalidatable"]
    cats_body = "[" + ",".join(cats) + "]"
    read = (
        f"{{enabled:{str(read_enabled).lower()},"
        f"categories:{cats_body},"
        f"invalidation_first:{str(invalidation_first).lower()}}}"
    )
    write = f"{{enabled:{str(write_enabled).lower()}}}"
    ttl_body = ",".join(f"{k}:{v}" for k, v in (ttl or {"d": 7}).items())
    _patch(
        run,
        f"cache.enabled={str(enabled).lower()}",
        f"cache.read={read}",
        f"cache.write={write}",
        f"cache.ttl={{{ttl_body}}}",
    )


def _simulate_new_process() -> None:
    remote._CACHE_PATH_BY_URL.clear()
    remote._URL_BY_CACHE_PATH.clear()


URL_A = "https://raw.githubusercontent.com/o/r/main/openapi.yaml"
URL_B = "https://example.invalid/some/other/openapi.yaml"


def test_master_switch_off_no_reads_no_writes(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`cache.enabled=false` → no persistent reads, no writes; per-process
    fallback only."""
    _set_cache(run, enabled=False)
    p1 = remote.fetch(URL_A)
    _simulate_new_process()
    p2 = remote.fetch(URL_A)
    # Two simulated processes both hit the network.
    assert len(fake_fetch["log"]) == 2
    # No persistent state on disk.
    assert not cache_root.exists()
    assert p1.read_bytes() == b"hello\n"
    assert p2.read_bytes() == b"hello\n"


def test_categories_invalidatable_only_skips_non_invalidatable_entries(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`categories=[invalidatable]` rejects entries with no cheap-check
    invalidator (ETag/Last-Modified don't qualify — server can return the
    full body on a conditional GET, which is no cheaper than a normal
    fetch). With nothing currently classified as invalidatable, this is
    equivalent to read-off."""
    fake_fetch["headers"][URL_A] = {"ETag": '"v1"'}
    _set_cache(run, categories=["invalidatable"], invalidation_first=False)
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    _simulate_new_process()
    remote.fetch(URL_A)
    # ETag does NOT classify as invalidatable → category mismatch → miss.
    assert len(fake_fetch["log"]) == 2


def test_categories_all_serves_any_cached_entry(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`categories=[all]` serves any cached entry within TTL, regardless of
    invalidator availability — no conditional GET, no network round trip."""
    _set_cache(run, categories=["all"], invalidation_first=False)
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    _simulate_new_process()
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1  # served from disk


def test_categories_all_with_invalidation_first_still_skips_network(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`invalidation_first=true` is a no-op for non-invalidatable entries.
    With `categories=[all]` and an ETag-only entry (which classifies as
    `all`, not `invalidatable`), the second process serves the cached blob
    without any conditional GET. This is the contract that lets `make
    ci-cached` survive a flaky upstream."""
    fake_fetch["headers"][URL_A] = {"ETag": '"v1"'}
    _set_cache(run, categories=["all"])  # invalidation_first=true by default
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    _simulate_new_process()
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1  # no revalidation call


def test_etag_and_last_modified_are_still_captured(
    cache_root: Path, fake_fetch, run,
) -> None:
    """The schema persists ETag/Last-Modified into the `invalidation`
    subobject for future use, even though they don't currently make an
    entry invalidatable."""
    fake_fetch["headers"][URL_A] = {"ETag": '"v1"', "Last-Modified": "yes"}
    _set_cache(run, categories=["all"])
    remote.fetch(URL_A)
    entry = json.loads(remote._url_index_path(URL_A).read_text())
    assert entry["invalidation"] == {
        "etag_http_header": '"v1"',
        "last_modified_http_header": "yes",
    }


def test_revalidation_failure_falls_back_to_cached_blob(
    cache_root: Path, fake_fetch, run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a cheap-check invalidator is present AND the revalidation call
    fails (timeout/5xx/DNS), the cached blob is served anyway. The TTL is
    the user's stated acceptance of staleness in exchange for resilience.

    No production codepath classifies entries as `invalidatable` yet, so
    we monkeypatch `_classify` to force the path under test. This locks
    in the fallback behavior for whenever a real cheap-check invalidator
    (e.g. `commit_sha1` via the forge commits API) lands."""
    monkeypatch.setattr(remote, "_classify", lambda inv: "invalidatable")
    _set_cache(run, categories=["invalidatable"])
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1

    call_count = [0]
    original_stub = remote._attempt_fetch

    def failing_stub(url, auth_host, source=None, conditional=None):
        call_count[0] += 1
        if conditional is not None:
            raise SystemExit(f"fetch timed out after 10s\n  ref to: {url}")
        return original_stub(url, auth_host, source=source, conditional=conditional)

    monkeypatch.setattr(remote, "_attempt_fetch", failing_stub)
    _simulate_new_process()
    p = remote.fetch(URL_A)
    assert call_count[0] == 1
    assert p.read_bytes() == b"hello\n"


def test_write_only_mode_populates_but_never_reads(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`read.enabled=false, write.enabled=true`: every call hits the network
    but each fetch refreshes the on-disk entry."""
    _set_cache(run, read_enabled=False)
    remote.fetch(URL_A)
    assert (cache_root / "index").is_dir()
    assert len(list((cache_root / "blobs").iterdir())) == 1
    _simulate_new_process()
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 2  # network was hit twice
    # Still one blob (same content), index entry was overwritten.
    assert len(list((cache_root / "blobs").iterdir())) == 1


def test_write_disabled_skips_persistent_store(
    cache_root: Path, fake_fetch, run,
) -> None:
    """`write.enabled=false` → no persistent writes; the in-process scratch
    file still works for the current run."""
    _set_cache(run, write_enabled=False)
    p = remote.fetch(URL_A)
    assert p.read_bytes() == b"hello\n"
    # No persistent dir was ever created.
    assert not cache_root.exists()


def test_ttl_expired_triggers_refetch(
    cache_root: Path, fake_fetch, run,
) -> None:
    _set_cache(run, categories=["all"], ttl={"s": 1}, invalidation_first=False)
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 1
    _simulate_new_process()
    idx = remote._url_index_path(URL_A)
    entry = json.loads(idx.read_text())
    entry["invalidation_ts"] = int(time.time()) - 999
    idx.write_text(json.dumps(entry) + "\n")
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 2  # expired → re-fetch
    refreshed = json.loads(idx.read_text())
    assert refreshed["invalidation_ts"] > entry["invalidation_ts"]


def test_blob_missing_triggers_refetch(
    cache_root: Path, fake_fetch, run,
) -> None:
    _set_cache(run, categories=["all"], invalidation_first=False)
    remote.fetch(URL_A)
    blobs = list((cache_root / "blobs").iterdir())
    blobs[0].unlink()
    _simulate_new_process()
    remote.fetch(URL_A)
    assert len(fake_fetch["log"]) == 2
    assert len(list((cache_root / "blobs").iterdir())) == 1


def test_content_sharing_dedupes_blob(
    cache_root: Path, fake_fetch, run,
) -> None:
    _set_cache(run, categories=["all"], invalidation_first=False)
    fake_fetch["responses"][URL_A] = b"same-bytes"
    fake_fetch["responses"][URL_B] = b"same-bytes"
    p1 = remote.fetch(URL_A)
    _simulate_new_process()
    p2 = remote.fetch(URL_B)
    assert p1.read_bytes() == p2.read_bytes() == b"same-bytes"
    assert len(list((cache_root / "blobs").iterdir())) == 1
    assert len(list((cache_root / "index").iterdir())) == 2


def test_returned_path_preserves_yaml_suffix(
    cache_root: Path, fake_fetch, run,
) -> None:
    _set_cache(run, categories=["all"], invalidation_first=False)
    p = remote.fetch(URL_A)
    assert p.suffix == ".yaml"
    _simulate_new_process()
    p2 = remote.fetch(URL_A)
    assert p2.suffix == ".yaml"


def test_legacy_index_entry_is_treated_as_miss(
    cache_root: Path, fake_fetch, run,
) -> None:
    """A pre-existing entry written in the old `{url, blob, ts, basename}`
    shape (i.e. no `blob_sha256`/`source`) is rejected and re-fetched."""
    _set_cache(run)
    # Manually inject an old-shape entry.
    cache_root.mkdir(parents=True)
    (cache_root / "index").mkdir()
    (cache_root / "blobs").mkdir()
    legacy_idx = remote._url_index_path(URL_A)
    legacy_idx.parent.mkdir(parents=True, exist_ok=True)
    legacy_idx.write_text(json.dumps({
        "url": URL_A, "blob": "fakehex", "ts": int(time.time()),
        "basename": "openapi.yaml",
    }) + "\n")
    remote.fetch(URL_A)
    # Network was hit (legacy shape treated as miss).
    assert len(fake_fetch["log"]) == 1
    # The legacy entry was overwritten with the new shape.
    fresh = json.loads(legacy_idx.read_text())
    assert "blob_sha256" in fresh and "source" in fresh
    assert "blob" not in fresh and "url" not in fresh


def test_with_override_enables_cache_transiently(
    cache_root: Path, fake_fetch, run, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cg with config defaults patch cache.enabled=true -- <cmd>` enables
    the cache for the foreign command via CG_DEFAULTS_CONFIG."""
    import os
    import sys

    # Real on-disk config: cache off.
    _set_cache(run, enabled=False)

    repo_root = str(Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        repo_root + (os.pathsep + existing if existing else ""),
    )

    out_file = tmp_path / "out.txt"
    write_script = tmp_path / "write.py"
    write_script.write_text(
        f"import json\n"
        f"from cg import config\n"
        f"s = config.cache_settings()\n"
        f"with open({str(out_file)!r}, 'w') as f:\n"
        f"    json.dump({{'read_enabled': s.read_enabled, 'write_enabled': s.write_enabled}}, f)\n"
    )
    code, _, _ = run(
        "with", "config", "defaults", "patch", "cache.enabled=true",
        "--", sys.executable, str(write_script),
    )
    assert code == 0
    seen = json.loads(out_file.read_text())
    assert seen["read_enabled"] is True
    assert seen["write_enabled"] is True
    # Parent process: master switch still off.
    assert config.cache_settings().read_enabled is False
    assert config.cache_settings().write_enabled is False
