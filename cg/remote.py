"""Remote-source support for codegen.

Lets $ref values and volume specifications use HTTP(S) URLs in addition to
local filesystem paths. Remote files are fetched once into a per-process
cache directory and then handled by the rest of the pipeline as ordinary
local Paths. A reverse map (cache_path -> source_url) lets volume matching
and header generation reason about the original URL when needed.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


_CACHE_DIR: Path | None = None
_URL_BY_CACHE_PATH: dict[Path, str] = {}
_CACHE_PATH_BY_URL: dict[str, Path] = {}


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def normalize_url(url: str) -> str:
    """Rewrite a github.com/<owner>/<repo>/<rest> URL to the raw form.

    Plain github.com URLs without `/blob/<ref>/` are interpreted as pointing
    at the default branch (`main`). URLs that already point at raw content,
    or that include `/blob/<ref>/`, are passed through.
    """
    if not is_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        return url
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3:
        return url
    owner, repo, *rest = parts
    if rest and rest[0] in ("blob", "raw"):
        # github.com/owner/repo/blob/<ref>/<path> -> raw.githubusercontent.com/owner/repo/<ref>/<path>
        ref_and_path = rest[1:]
        new_path = "/".join([owner, repo, *ref_and_path])
        return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))
    # Convenience form: assume default branch `main`.
    new_path = "/".join([owner, repo, "main", *rest])
    return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.mkdtemp(prefix="cg-remote-"))
    return _CACHE_DIR


def fetch(url: str) -> Path:
    """Download `url` (after normalization) into the cache and return the local path.

    Subsequent calls for the same URL return the cached path without re-downloading.
    The cache file name preserves the original `.yaml` suffix so downstream
    assertions on `Path.suffix` keep working.
    """
    canonical = normalize_url(url)
    if canonical in _CACHE_PATH_BY_URL:
        return _CACHE_PATH_BY_URL[canonical]

    parsed = urllib.parse.urlparse(canonical)
    basename = os.path.basename(parsed.path) or "remote.yaml"
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    local = _cache_dir() / f"{digest}-{basename}"

    req = urllib.request.Request(canonical, headers={"User-Agent": "cg-codegen"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    local.write_bytes(data)

    _CACHE_PATH_BY_URL[canonical] = local
    _URL_BY_CACHE_PATH[local] = canonical
    return local


def url_of_cache_path(path: Path) -> str | None:
    return _URL_BY_CACHE_PATH.get(path)


def is_cache_path(path: Path) -> bool:
    return path in _URL_BY_CACHE_PATH


def parse_volume(raw: str) -> tuple[str | Path, Path]:
    """Parse a `src:dst` volume spec, accepting URL or local sources.

    Returns `(src, dst)` where `src` is a URL string (if it starts with
    http:// or https://) or an absolute Path (resolved relative to cwd);
    `dst` is always a local Path resolved relative to cwd.
    """
    if is_url(raw):
        # Find the `:` separating LHS URL from RHS local path. The URL
        # itself contains `://`, so skip past it and find the next `:`.
        scheme_end = raw.find("://")
        sep = raw.find(":", scheme_end + 3)
        if sep == -1:
            raise SystemExit(f"volume spec missing local destination: {raw!r}")
        src_url = normalize_url(raw[:sep])
        dst_raw = raw[sep + 1 :]
        dst = Path(os.path.expanduser(dst_raw))
        if not dst.is_absolute():
            dst = Path.cwd() / dst
        return src_url, dst
    src_raw, dst_raw = raw.split(":", 1)
    return Path.cwd() / src_raw, Path.cwd() / dst_raw
