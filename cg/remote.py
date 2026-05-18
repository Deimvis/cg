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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


_CACHE_DIR: Path | None = None
_URL_BY_CACHE_PATH: dict[Path, str] = {}
_CACHE_PATH_BY_URL: dict[str, Path] = {}


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _looks_like_gitlab(parsed: urllib.parse.ParseResult) -> bool:
    """Heuristic for self-hosted and gitlab.com URLs."""
    if parsed.netloc == "gitlab.com" or parsed.netloc.startswith("gitlab."):
        return True
    # Any URL using the `/-/blob/` or `/-/raw/` convention is a GitLab URL,
    # even on hosts that don't advertise themselves as gitlab.
    return "/-/blob/" in parsed.path or "/-/raw/" in parsed.path


_DEFAULT_BRANCHES = ("main", "master")


def _normalize_github(parsed: urllib.parse.ParseResult, default_ref: str = "main") -> str:
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3:
        return urllib.parse.urlunparse(parsed)
    owner, repo, *rest = parts
    if rest and rest[0] in ("blob", "raw"):
        ref_and_path = rest[1:]
        new_path = "/".join([owner, repo, *ref_and_path])
        return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))
    new_path = "/".join([owner, repo, default_ref, *rest])
    return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))


def _normalize_gitlab(parsed: urllib.parse.ParseResult, default_ref: str = "main") -> str:
    """Normalize gitlab.com (and self-hosted gitlab) URLs to their raw form.

    Accepts:
      - .../<namespace>/<repo>/-/blob/<ref>/<path> -> .../<namespace>/<repo>/-/raw/<ref>/<path>
      - .../<namespace>/<repo>/-/raw/<ref>/<path>  (passthrough)
      - .../<owner>/<repo>/<path>                   -> .../<owner>/<repo>/-/raw/<default_ref>/<path>

    For repos in nested groups (`group/sub/repo`), the convenience form is
    ambiguous; use the explicit `/-/blob/<ref>/...` or `/-/raw/<ref>/...`
    form so cg can find the project/path boundary.
    """
    parts = [p for p in parsed.path.split("/") if p]
    if "-" in parts:
        idx = parts.index("-")
        if idx >= 2 and idx + 2 < len(parts) and parts[idx + 1] in ("blob", "raw"):
            namespace = parts[:idx]
            kind, ref, *rest = parts[idx + 1:]
            new_parts = namespace + ["-", "raw", ref, *rest]
            return urllib.parse.urlunparse(parsed._replace(path="/" + "/".join(new_parts)))
        return urllib.parse.urlunparse(parsed)
    if len(parts) < 2:
        return urllib.parse.urlunparse(parsed)
    namespace_repo = parts[:2]
    rest = parts[2:]
    new_parts = namespace_repo + ["-", "raw", default_ref, *rest]
    return urllib.parse.urlunparse(parsed._replace(path="/" + "/".join(new_parts)))


def _is_convenience_form(url: str) -> bool:
    """True if the URL doesn't already encode an explicit ref and should
    therefore be retried against alternate default branches."""
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc == "github.com":
        # github.com/owner/repo/<rest>: convenience iff `rest[0]` isn't blob/raw
        return len(parts) >= 3 and parts[2] not in ("blob", "raw")
    if _looks_like_gitlab(parsed):
        # GitLab convenience form has no `/-/blob/` or `/-/raw/` marker
        return "-" not in parts
    return False


def normalize_url(url: str, default_ref: str = "main") -> str:
    """Rewrite a known forge URL to its raw-content form.

    - GitHub: github.com/<owner>/<repo>/[blob/<ref>/]<path> -> raw.githubusercontent.com/<owner>/<repo>/<ref-or-default>/<path>
    - GitLab: <host>/<owner>/<repo>/-/blob/<ref>/<path>     -> <host>/<owner>/<repo>/-/raw/<ref>/<path>
              <host>/<owner>/<repo>/<path>                   -> <host>/<owner>/<repo>/-/raw/<default_ref>/<path>
    Other URLs pass through unchanged.
    """
    if not is_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc == "github.com":
        return _normalize_github(parsed, default_ref)
    if _looks_like_gitlab(parsed):
        return _normalize_gitlab(parsed, default_ref)
    return url


def _apply_http_mirror(url: str) -> tuple[str, str]:
    """Return `(possibly_rewritten_url, original_host)`. If the URL's host
    has an http mirror configured in `~/.config/cg/openapi_providers.json`
    under `mirrors.http`, rewrite the URL to that mirror prefix while
    preserving the original path, query, and fragment. The second tuple
    element is always the *original* host so auth headers can still be
    keyed by it."""
    if not is_url(url):
        return url, urllib.parse.urlparse(url).netloc
    parsed = urllib.parse.urlparse(url)
    from . import config
    prefix = config.http_mirror_for(parsed.netloc)
    if prefix is None:
        return url, parsed.netloc
    prefix_parsed = urllib.parse.urlparse(prefix)
    prefix_path = prefix_parsed.path.rstrip("/")
    new_path = prefix_path + parsed.path
    new_url = urllib.parse.urlunparse((
        prefix_parsed.scheme,
        prefix_parsed.netloc,
        new_path,
        "",
        parsed.query,
        parsed.fragment,
    ))
    return new_url, parsed.netloc


def _candidate_canonical_urls(url: str) -> list[tuple[str, str]]:
    """Return the URL(s) to try fetching for `url`, in order, paired with
    the *auth host* to use for each. For convenience-form forge URLs (no
    explicit ref), returns `[main-form, master-form]` candidates."""
    rewritten, auth_host = _apply_http_mirror(url)
    canonical = normalize_url(rewritten)
    if not _is_convenience_form(url):
        return [(canonical, auth_host)]
    out: list[tuple[str, str]] = [(canonical, auth_host)]
    for ref in _DEFAULT_BRANCHES[1:]:
        alt = normalize_url(rewritten, default_ref=ref)
        if alt != canonical and not any(alt == c for c, _ in out):
            out.append((alt, auth_host))
    return out


def _gitlab_api_url(canonical: str) -> str | None:
    """Translate a normalized GitLab `/-/raw/<ref>/<path>` URL into the
    GitLab REST API form `/api/v4/projects/<id>/repository/files/<path>/raw?ref=<ref>`.

    Some corp GitLab deployments protect web routes (`/-/raw/...`) behind a
    browser session even when a valid `PRIVATE-TOKEN` is supplied; the REST
    API route honors the token. Returns None if `canonical` doesn't match
    the expected `/-/raw/` shape.
    """
    parsed = urllib.parse.urlparse(canonical)
    if not _looks_like_gitlab(parsed):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if "-" not in parts:
        return None
    idx = parts.index("-")
    if idx < 2 or idx + 2 >= len(parts) or parts[idx + 1] != "raw":
        return None
    namespace = parts[:idx]  # e.g. ['ai', 'godzen'] or ['group', 'sub', 'repo']
    ref = parts[idx + 2]
    file_path = "/".join(parts[idx + 3:])
    if not file_path:
        return None
    project = "/".join(namespace)
    encoded_project = urllib.parse.quote(project, safe="")
    encoded_path = urllib.parse.quote(file_path, safe="")
    encoded_ref = urllib.parse.quote(ref, safe="")
    new_path = f"/api/v4/projects/{encoded_project}/repository/files/{encoded_path}/raw"
    return urllib.parse.urlunparse(
        parsed._replace(path=new_path, query=f"ref={encoded_ref}")
    )


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.mkdtemp(prefix="cg-remote-"))
    return _CACHE_DIR


class _NotFound(Exception):
    """Internal: fetch attempt got a 404 (used for ref-fallback in convenience URLs)."""


def _attempt_fetch(canonical: str, auth_host: str) -> tuple[bytes, str]:
    """Single fetch of `canonical`. Returns `(data, content_type)`. Raises
    `_NotFound` on 404, or `SystemExit` for non-recoverable failures.

    `auth_host` is the *original* host (pre-mirror) used to look up auth
    credentials. The actual request goes to `canonical`. When `auth_host`
    differs from the request host, the GitLab REST-API rewrite is skipped
    (mirrors typically don't expose `/api/v4`)."""
    parsed = urllib.parse.urlparse(canonical)
    auth_parsed = parsed._replace(netloc=auth_host)
    headers = {"User-Agent": "cg-codegen"}
    auth_headers = _auth_headers_for(auth_parsed)
    sent_token = bool(auth_headers)
    for header_name, value in auth_headers.items():
        headers[header_name] = value

    request_url = canonical
    if sent_token and auth_host == parsed.netloc and _looks_like_gitlab(parsed):
        api_url = _gitlab_api_url(canonical)
        if api_url is not None:
            request_url = api_url

    req = urllib.request.Request(request_url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound()
        if e.code in (401, 403):
            raise SystemExit(
                f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.\n"
                f"{_auth_hint(auth_parsed, sent_token=sent_token, status=e.code)}"
            )
        raise SystemExit(f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.")

    basename = os.path.basename(parsed.path) or "remote.yaml"
    if "html" in content_type.lower() and basename.endswith(".yaml"):
        raise SystemExit(
            f"fetch failed: {canonical} returned HTML (Content-Type: {content_type!r}, "
            f"final URL: {final_url}).\n"
            f"{_auth_hint(auth_parsed, sent_token=sent_token, status=200)}"
        )
    return data, content_type


def fetch(url: str) -> Path:
    """Download `url` (after normalization) into the cache and return the local path.

    Subsequent calls for the same URL return the cached path without re-downloading.
    For convenience-form forge URLs without an explicit ref, attempts `main`
    first and falls back to `master` on 404. The cache file name preserves
    the original `.yaml` suffix so downstream assertions on `Path.suffix`
    keep working.
    """
    candidates = _candidate_canonical_urls(url)
    # Reuse the cache via any of the candidate canonical URLs.
    for c, _ in candidates:
        if c in _CACHE_PATH_BY_URL:
            return _CACHE_PATH_BY_URL[c]

    last_404: tuple[str, str] | None = None
    data: bytes | None = None
    canonical: str | None = None
    for c, auth_host in candidates:
        try:
            data, _ = _attempt_fetch(c, auth_host)
            canonical = c
            break
        except _NotFound:
            last_404 = (c, auth_host)
            continue

    if data is None or canonical is None:
        # All candidates 404'd. Report against the last attempt (typically
        # the master fallback) with full forge auth context, keyed on the
        # *auth* host (so the hint references github.com, not a mirror).
        last_url, last_auth_host = last_404 or candidates[-1]
        auth_parsed = urllib.parse.urlparse(last_url)._replace(netloc=last_auth_host)
        sent_token = bool(_auth_headers_for(auth_parsed))
        attempted = [c for c, _ in candidates]
        raise SystemExit(
            f"fetch failed: all candidate URLs 404'd: {attempted}\n"
            f"{_auth_hint(auth_parsed, sent_token=sent_token, status=404)}"
        )

    parsed = urllib.parse.urlparse(canonical)
    basename = os.path.basename(parsed.path) or "remote.yaml"
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    local = _cache_dir() / f"{digest}-{basename}"
    local.write_bytes(data)

    _CACHE_PATH_BY_URL[canonical] = local
    _URL_BY_CACHE_PATH[local] = canonical
    return local


def _token_for(netloc: str) -> str | None:
    """Resolve the auth token for `netloc` from `~/.config/cg/openapi_providers.json`."""
    from . import config
    return config.token_for_domain(netloc)


def _auth_headers_for(parsed: urllib.parse.ParseResult) -> dict[str, str]:
    """Auth headers to send with a request, based on the target host."""
    netloc = parsed.netloc
    token = _token_for(netloc)
    if not token:
        return {}
    if netloc == "gitlab.com" or netloc.startswith("gitlab."):
        return {"PRIVATE-TOKEN": token}
    if netloc in ("github.com", "raw.githubusercontent.com"):
        return {"Authorization": f"Bearer {token}"}
    # Unknown forge but a token was configured for this host; default to
    # the Bearer scheme (works for most APIs).
    return {"Authorization": f"Bearer {token}"}


def _auth_hint(parsed: urllib.parse.ParseResult, *, sent_token: bool = False, status: int = 0) -> str:
    """Build an actionable hint for an auth-related fetch failure. The text
    differs depending on whether we sent a token at all and which status the
    server returned."""
    netloc = parsed.netloc
    is_gitlab = netloc == "gitlab.com" or netloc.startswith("gitlab.")
    is_github = netloc in ("github.com", "raw.githubusercontent.com")
    forge = "gitlab" if is_gitlab else ("github" if is_github else None)
    scope = "read_repository" if is_gitlab else ("repo" if is_github else "read")

    setup_lines = [
        f"  cg config providers add                # interactive, saves to ~/.config/cg",
        f"  cg config providers add <provider> <domain> <token>   # non-interactive (CI)",
    ]

    if sent_token:
        head = (
            f"a token is configured for {netloc!r} but the server rejected it "
            f"(HTTP {status}). Check that the token is valid and has the "
            f"`{scope}` scope, and that it can access this specific repository/path."
        )
        if forge:
            head += (
                f" If you need to use a different token for this host, update it with "
                f"`cg config providers add`."
            )
        return head
    head = (
        f"the request was unauthenticated and the server returned HTTP {status}. "
        f"If this repository is private, configure a token for {netloc!r}:"
    )
    return head + "\n" + "\n".join(setup_lines)


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


def _coerce_dst(raw: str, render=None) -> Path | None:
    if render is not None:
        raw = render(raw)
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


def parse_volume(raw: str, render_dst=None) -> dict:
    """Parse a volume spec into a dict with keys `src`, `dst`, `read_only`,
    `checks`.

    Short form: `src:dst`, optionally suffixed with `:ro` and/or `:ro+nocheck`.
    Long form (triggered when raw contains `,`):
      `src="...",dst="...",read_only=true,checks=null`

    If `render_dst` is provided, it's invoked on the raw dst string before
    Path coercion (used to expand jinja templates like `{{output_type}}`).
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
            "dst": _coerce_dst(str(spec["dst"]), render_dst),
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
        "dst": _coerce_dst(dst_raw, render_dst),
        "read_only": read_only,
        "checks": None,
    }
