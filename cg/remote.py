"""Remote-source support for codegen.

Lets $ref values and volume specifications use HTTP(S) URLs in addition to
local filesystem paths. Remote files are fetched once into a per-process
cache directory and then handled by the rest of the pipeline as ordinary
local Paths. A reverse map (cache_path -> source_url) lets volume matching
and header generation reason about the original URL when needed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import tempfile
import time
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


def _template_vars(parsed: urllib.parse.ParseResult) -> dict[str, str]:
    """Variable map for `mirrors.custom` rewrite templates.

    Names match the user-facing schema verbatim — `schema` (not `scheme`)
    and `hostname` (not `host`).
    """
    return {
        "schema":   parsed.scheme,
        "hostname": parsed.hostname or "",
        "port":     str(parsed.port) if parsed.port else "",
        "path":     parsed.path,
        "query":    parsed.query,
    }


def _apply_custom_mirror(url: str, normalized: str) -> tuple[str, str] | None:
    """Try every `mirrors.custom` rule in declaration order.

    Returns `(rewritten_url, auth_host)` on first match, or None.
    `auth_host` is the *original* URL's netloc, so token lookups continue
    to key off the source forge. For `convertable` rules, template vars
    come from the normalized URL (post `normalize_url`); for `exact`
    rules, from `url` as-is.
    """
    from . import config
    rules = config.custom_mirrors()
    if not rules:
        return None
    parsed_input = urllib.parse.urlparse(url)
    parsed_norm = urllib.parse.urlparse(normalized)
    auth_host = parsed_input.netloc
    for rule in rules:
        match = rule["match"]
        if "exact" in match:
            eq = match["exact"]["hostname"]["eq"]
            if (parsed_input.hostname or "") != eq:
                continue
            vars_src = parsed_input
        else:
            eq = match["convertable"]["hostname"]["eq"]
            if (parsed_norm.hostname or "") != eq:
                continue
            vars_src = parsed_norm
        template = rule["rewrite"]["url"]["template"]
        try:
            rewritten = template.format_map(_template_vars(vars_src))
        except (KeyError, IndexError, ValueError) as e:
            raise SystemExit(
                f"mirrors.custom[{rule['name']!r}]: template expansion failed for "
                f"{url!r}: {e}"
            )
        return rewritten, auth_host
    return None


def _candidate_canonical_urls(url: str) -> list[tuple[str, str]]:
    """Return the URL(s) to try fetching for `url`, in order, paired with
    the *auth host* to use for each. For convenience-form forge URLs (no
    explicit ref), returns `[main-form, master-form]` candidates.

    Routing order: `mirrors.custom` rules are evaluated first (against the
    pre- and post-normalize URL depending on the rule kind). If none match,
    fall back to the historical `mirrors.http` lookup applied to the input
    URL before normalization.
    """
    normalized = normalize_url(url)
    custom = _apply_custom_mirror(url, normalized)
    if custom is not None:
        canonical, auth_host = custom
    else:
        rewritten, auth_host = _apply_http_mirror(url)
        canonical = normalize_url(rewritten)
    if not _is_convenience_form(url):
        return [(canonical, auth_host)]
    out: list[tuple[str, str]] = [(canonical, auth_host)]
    for ref in _DEFAULT_BRANCHES[1:]:
        if custom is not None:
            alt_norm = normalize_url(url, default_ref=ref)
            alt_match = _apply_custom_mirror(url, alt_norm)
            alt = alt_match[0] if alt_match is not None else alt_norm
        else:
            rewritten_alt, _ = _apply_http_mirror(url)
            alt = normalize_url(rewritten_alt, default_ref=ref)
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


_FETCH_TIMEOUT_S = 10
_FETCH_MAX_ATTEMPTS = 3
_FETCH_RETRY_BACKOFF_S = (0.5, 1.5)


def _dial(host: str, port: int, mode: str, timeout):
    """Open a TCP socket to (host, port), honoring an `ip_resolution` mode.

    Modes: 'any' (default — delegates to socket.create_connection),
    'prefer/ipv4'|'prefer/ipv6' (try preferred family first, fall back),
    'only/ipv4'|'only/ipv6' (refuse to connect over the other family).
    """
    if mode == "any":
        return socket.create_connection((host, port), timeout=timeout)
    family_pref = socket.AF_INET if "ipv4" in mode else socket.AF_INET6
    only = mode.startswith("only/")
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if only:
        infos = [i for i in infos if i[0] == family_pref]
        if not infos:
            label = "IPv4" if family_pref == socket.AF_INET else "IPv6"
            raise OSError(
                f"no {label} address for {host!r} (ip_resolution={mode!r})"
            )
    else:
        infos.sort(key=lambda i: 0 if i[0] == family_pref else 1)
    last_err: OSError | None = None
    for fam, kind, proto, _canon, sa in infos:
        s = socket.socket(fam, kind, proto)
        try:
            s.settimeout(timeout)
            s.connect(sa)
            return s
        except OSError as e:
            last_err = e
            s.close()
    raise last_err or OSError(f"could not connect to {host}:{port}")


def _make_connection_cls(base, mode: str):
    class _IPFilteredConnection(base):
        def connect(self):
            self.sock = _dial(self.host, self.port, mode, self.timeout)
            tunnel_host = getattr(self, "_tunnel_host", None)
            if tunnel_host:
                self._tunnel()
            ctx = getattr(self, "_context", None)
            if ctx is not None:
                self.sock = ctx.wrap_socket(
                    self.sock, server_hostname=tunnel_host or self.host
                )
    return _IPFilteredConnection


def _opener_for_mode(mode: str) -> urllib.request.OpenerDirector:
    """Return an opener that enforces the given `ip_resolution` mode.

    For `mode == "any"` returns the stock default opener so the
    historical request path is preserved exactly.
    """
    if mode == "any":
        return urllib.request.build_opener()
    http_cls = _make_connection_cls(http.client.HTTPConnection, mode)
    https_cls = _make_connection_cls(http.client.HTTPSConnection, mode)

    class _H(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_cls, req)

    class _HS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_cls, req)

    return urllib.request.build_opener(_H(), _HS())


class _NotFound(Exception):
    """Internal: fetch attempt got a 404 (used for ref-fallback in convenience URLs)."""


def _format_source(source: str | Path | None) -> str | None:
    """Render a source location for error messages. If `source` is a cached
    remote path, replace it with the original URL so users see something
    they can actually open."""
    if source is None:
        return None
    if isinstance(source, Path):
        url = url_of_cache_path(source)
        return url if url is not None else str(source)
    return str(source)


def _attempt_fetch(canonical: str, auth_host: str, source: str | Path | None = None) -> tuple[bytes, str]:
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

    from . import config
    request_url = canonical
    if auth_host == parsed.netloc and _looks_like_gitlab(parsed):
        if config.gitlab_fetch_mode(auth_host) == "api":
            api_url = _gitlab_api_url(canonical)
            if api_url is not None:
                request_url = api_url

    req = urllib.request.Request(request_url, headers=headers)
    connect_host = urllib.parse.urlparse(request_url).netloc
    opener = _opener_for_mode(config.ip_resolution_for_domain(connect_host))
    data: bytes = b""
    content_type = ""
    final_url = request_url
    for attempt in range(_FETCH_MAX_ATTEMPTS):
        try:
            with opener.open(req, timeout=_FETCH_TIMEOUT_S) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                final_url = resp.geturl()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise _NotFound()
            if e.code in (401, 403):
                raise SystemExit(
                    f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.\n"
                    f"{_auth_hint(auth_parsed, sent_token=sent_token, status=e.code)}"
                )
            raise SystemExit(f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.")
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            is_timeout = isinstance(e, TimeoutError) or isinstance(reason, TimeoutError)
            if is_timeout or attempt == _FETCH_MAX_ATTEMPTS - 1:
                lines = []
                ref_from = _format_source(source)
                if ref_from is not None:
                    lines.append(f"  ref from:  {ref_from}")
                lines.append(f"  ref to:    {canonical}")
                if request_url != canonical:
                    lines.append(f"  requested: {request_url}")
                lines.append(f"  host:      {connect_host}")
                if not is_timeout and attempt > 0:
                    lines.append(f"  attempts:  {attempt + 1}")
                details = "\n".join(lines)
                if is_timeout:
                    raise SystemExit(
                        f"fetch timed out after {_FETCH_TIMEOUT_S}s\n{details}"
                    )
                raise SystemExit(
                    f"fetch failed: {type(e).__name__}: {reason}\n{details}"
                )
            time.sleep(_FETCH_RETRY_BACKOFF_S[attempt])

    basename = os.path.basename(parsed.path) or "remote.yaml"
    if "html" in content_type.lower() and basename.endswith(".yaml"):
        raise SystemExit(
            f"fetch failed: {canonical} returned HTML (Content-Type: {content_type!r}, "
            f"final URL: {final_url}).\n"
            f"{_auth_hint(auth_parsed, sent_token=sent_token, status=200)}"
        )
    return data, content_type


def _persistent_cache_root() -> Path:
    from . import config
    return config.cache_dir() / "refs"


def _url_index_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return _persistent_cache_root() / "index" / f"{digest}.json"


def _blob_path_for(data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    return _persistent_cache_root() / "blobs" / digest


def _ensure_cache_dirs() -> None:
    root = _persistent_cache_root()
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    (root / "index").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root.parent, 0o700)
        os.chmod(root, 0o700)
    except OSError:
        pass


def _load_index_entry(url: str) -> dict | None:
    path = _url_index_path(url)
    if not path.exists():
        return None
    try:
        with path.open("r") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("url") != url:
        # SHA-1 collision (or stale entry from a renamed URL scheme); miss.
        return None
    return entry


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp.{os.getpid()}.{path.name}"
    with tmp.open("wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _materialize_scratch(blob: Path, basename: str) -> Path:
    """Return a path in the per-process scratch dir whose contents are the
    cached blob. Uses a hardlink so the inode is shared (no copy cost); falls
    back to a byte copy on cross-device or unsupported filesystems."""
    digest_short = blob.name[:12]
    scratch = _cache_dir() / f"{digest_short}-{basename}"
    if scratch.exists():
        try:
            scratch.unlink()
        except OSError:
            pass
    try:
        os.link(blob, scratch)
    except OSError:
        shutil.copyfile(blob, scratch)
    return scratch


def _lookup_persistent(url: str, ttl_s: int) -> Path | None:
    """Return a scratch path materialized from the cached blob if the URL has
    a fresh entry whose blob still exists on disk. Else None."""
    entry = _load_index_entry(url)
    if entry is None:
        return None
    ts = entry.get("ts")
    blob_hex = entry.get("blob")
    basename = entry.get("basename") or "remote.yaml"
    if not isinstance(ts, (int, float)) or not isinstance(blob_hex, str):
        return None
    if time.time() - ts > ttl_s:
        return None
    blob = _persistent_cache_root() / "blobs" / blob_hex
    if not blob.exists():
        return None
    return _materialize_scratch(blob, basename)


def _store_persistent(url: str, data: bytes, basename: str) -> Path:
    """Write `data` to the blob store (if absent) and update the URL index.
    Returns the blob path."""
    _ensure_cache_dirs()
    blob = _blob_path_for(data)
    if not blob.exists():
        _atomic_write_bytes(blob, data)
    entry = {
        "url": url,
        "blob": blob.name,
        "ts": int(time.time()),
        "basename": basename,
    }
    _atomic_write_bytes(
        _url_index_path(url),
        (json.dumps(entry) + "\n").encode("utf-8"),
    )
    return blob


def fetch(url: str, source: str | Path | None = None) -> Path:
    """Download `url` (after normalization) into the cache and return the local path.

    Subsequent calls for the same URL return the cached path without re-downloading.
    For convenience-form forge URLs without an explicit ref, attempts `main`
    first and falls back to `master` on 404. The cache file name preserves
    the original `.yaml` suffix so downstream assertions on `Path.suffix`
    keep working.

    `source`, when given, is the file/URL that referenced `url` (e.g. the
    openapi spec containing a `$ref`); it's surfaced in fetch error
    messages so users can locate the bad reference.
    """
    candidates = _candidate_canonical_urls(url)
    # Reuse the cache via any of the candidate canonical URLs.
    for c, _ in candidates:
        if c in _CACHE_PATH_BY_URL:
            return _CACHE_PATH_BY_URL[c]

    from . import config
    cache_enabled, cache_ttl_s = config.cache_settings()
    if cache_enabled:
        for c, _ in candidates:
            hit = _lookup_persistent(c, cache_ttl_s)
            if hit is not None:
                _CACHE_PATH_BY_URL[c] = hit
                _URL_BY_CACHE_PATH[hit] = c
                return hit

    last_404: tuple[str, str] | None = None
    data: bytes | None = None
    canonical: str | None = None
    for c, auth_host in candidates:
        try:
            data, _ = _attempt_fetch(c, auth_host, source=source)
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
        ref_from = _format_source(source)
        ref_line = f"\n  ref from: {ref_from}" if ref_from else ""
        raise SystemExit(
            f"fetch failed: all candidate URLs 404'd: {attempted}{ref_line}\n"
            f"{_auth_hint(auth_parsed, sent_token=sent_token, status=404)}"
        )

    parsed = urllib.parse.urlparse(canonical)
    basename = os.path.basename(parsed.path) or "remote.yaml"
    if cache_enabled:
        blob = _store_persistent(canonical, data, basename)
        local = _materialize_scratch(blob, basename)
    else:
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
