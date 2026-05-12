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


_VOLUME_ALLOWED_KEYS = {"src", "dst", "read_only", "checks"}


def _coerce_local_path(raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else Path.cwd() / p


def _coerce_src(raw: str) -> str | Path:
    if is_url(raw):
        return normalize_url(raw)
    return _coerce_local_path(raw)


def _coerce_dst(raw: str) -> Path | None:
    if raw == "-":
        return None
    return _coerce_local_path(raw)


def _parse_long_form(raw: str) -> dict:
    """Parse `src="...",dst="...",read_only=true,checks=null` into a dict."""
    out: dict = {}
    # Split on commas at top level, respecting double-quoted segments.
    parts: list[str] = []
    buf, in_quote = "", False
    for ch in raw:
        if ch == '"':
            in_quote = not in_quote
            buf += ch
        elif ch == "," and not in_quote:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"volume spec entry missing '=': {part!r}")
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value == "true":
            value = True
        elif value == "false":
            value = False
        elif value == "null":
            value = None
        out[key] = value
    return out


def parse_volume(raw: str) -> dict:
    """Parse a volume spec into a dict with keys `src`, `dst`, `read_only`,
    `checks`.

    Short form: `src:dst`, optionally suffixed with `:ro` and/or `:ro+nocheck`.
    Long form (triggered when raw contains `,`):
      `src="...",dst="...",read_only=true,checks=null`
    """
    if "," in raw:
        spec = _parse_long_form(raw)
        unknown = set(spec.keys()) - _VOLUME_ALLOWED_KEYS
        if unknown:
            raise SystemExit(f"volume spec has unknown keys: {sorted(unknown)}")
        if "src" not in spec or "dst" not in spec:
            raise SystemExit(f"volume spec must contain src and dst: {raw!r}")
        read_only = spec.get("read_only", False)
        if not isinstance(read_only, bool):
            raise SystemExit(f"volume spec read_only must be true/false, got {read_only!r}")
        if "checks" in spec and not read_only:
            raise SystemExit("volume spec: checks is only valid when read_only=true")
        if spec.get("checks", None) is not None:
            raise SystemExit("volume spec: only checks=null is supported for now")
        return {
            "src": _coerce_src(str(spec["src"])),
            "dst": _coerce_dst(str(spec["dst"])),
            "read_only": read_only,
            "checks": None,
        }

    # Short form. Strip well-known trailing options.
    read_only = False
    nocheck = False
    while True:
        if raw.endswith(":ro+nocheck"):
            read_only, nocheck = True, True
            raw = raw[: -len(":ro+nocheck")]
        elif raw.endswith(":ro"):
            read_only = True
            raw = raw[: -len(":ro")]
        else:
            break

    if "+nocheck" in raw and not read_only:
        raise SystemExit("volume spec: ':ro+nocheck' requires :ro")

    if is_url(raw):
        scheme_end = raw.find("://")
        sep = raw.find(":", scheme_end + 3)
        if sep == -1:
            raise SystemExit(f"volume spec missing local destination: {raw!r}")
        src = normalize_url(raw[:sep])
        dst_raw = raw[sep + 1 :]
    else:
        if ":" not in raw:
            raise SystemExit(f"volume spec must be 'src:dst[:ro[+nocheck]]': {raw!r}")
        src_raw, dst_raw = raw.split(":", 1)
        src = _coerce_local_path(src_raw)

    return {
        "src": src,
        "dst": _coerce_dst(dst_raw),
        "read_only": read_only,
        "checks": None,
    }
