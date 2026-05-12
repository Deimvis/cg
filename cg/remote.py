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


def _normalize_github(parsed: urllib.parse.ParseResult) -> str:
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3:
        return urllib.parse.urlunparse(parsed)
    owner, repo, *rest = parts
    if rest and rest[0] in ("blob", "raw"):
        # github.com/owner/repo/blob/<ref>/<path> -> raw.githubusercontent.com/owner/repo/<ref>/<path>
        ref_and_path = rest[1:]
        new_path = "/".join([owner, repo, *ref_and_path])
        return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))
    # Convenience form: assume default branch `main`.
    new_path = "/".join([owner, repo, "main", *rest])
    return urllib.parse.urlunparse(parsed._replace(netloc="raw.githubusercontent.com", path="/" + new_path))


def _normalize_gitlab(parsed: urllib.parse.ParseResult) -> str:
    """Normalize gitlab.com (and self-hosted gitlab) URLs to their raw form.

    Accepts:
      - .../<namespace>/<repo>/-/blob/<ref>/<path> -> .../<namespace>/<repo>/-/raw/<ref>/<path>
      - .../<namespace>/<repo>/-/raw/<ref>/<path>  (passthrough)
      - .../<owner>/<repo>/<path>                   -> .../<owner>/<repo>/-/raw/main/<path>

    For repos in nested groups (`group/sub/repo`), the convenience form is
    ambiguous; use the explicit `/-/blob/<ref>/...` or `/-/raw/<ref>/...`
    form so cg can find the project/path boundary.
    """
    parts = [p for p in parsed.path.split("/") if p]
    # Find the `-` separator that splits namespace+repo from blob/raw+ref+path.
    if "-" in parts:
        idx = parts.index("-")
        if idx >= 2 and idx + 2 < len(parts) and parts[idx + 1] in ("blob", "raw"):
            namespace = parts[:idx]
            kind, ref, *rest = parts[idx + 1:]
            new_parts = namespace + ["-", "raw", ref, *rest]
            return urllib.parse.urlunparse(parsed._replace(path="/" + "/".join(new_parts)))
        return urllib.parse.urlunparse(parsed)
    # Convenience form with no `/-/blob/` marker: assume `main`.
    if len(parts) < 2:
        return urllib.parse.urlunparse(parsed)
    namespace_repo = parts[:2]
    rest = parts[2:]
    new_parts = namespace_repo + ["-", "raw", "main", *rest]
    return urllib.parse.urlunparse(parsed._replace(path="/" + "/".join(new_parts)))


def normalize_url(url: str) -> str:
    """Rewrite a known forge URL to its raw-content form.

    - GitHub: github.com/<owner>/<repo>/[blob/<ref>/]<path> -> raw.githubusercontent.com/<owner>/<repo>/<ref-or-main>/<path>
    - GitLab: <host>/<owner>/<repo>/-/blob/<ref>/<path>     -> <host>/<owner>/<repo>/-/raw/<ref>/<path>
              <host>/<owner>/<repo>/<path>                   -> <host>/<owner>/<repo>/-/raw/main/<path>
    Other URLs pass through unchanged.
    """
    if not is_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc == "github.com":
        return _normalize_github(parsed)
    if _looks_like_gitlab(parsed):
        return _normalize_gitlab(parsed)
    return url


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

    headers = {"User-Agent": "cg-codegen"}
    auth_headers = _auth_headers_for(parsed)
    sent_token = bool(auth_headers)
    for header_name, value in auth_headers.items():
        headers[header_name] = value

    # For GitLab with a configured token, fetch via the REST API instead of
    # the `/-/raw/` web route — some corp deployments protect web routes
    # behind a session and ignore `PRIVATE-TOKEN`, but honor it on the API.
    request_url = canonical
    if sent_token and _looks_like_gitlab(parsed):
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
        # 401/403/404 from any forge means we either need a token or the
        # token we sent doesn't grant access (or the path is wrong, which
        # for private repos GitHub reports as 404 indistinguishably).
        if e.code in (401, 403, 404):
            raise SystemExit(
                f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.\n"
                f"{_auth_hint(parsed, sent_token=sent_token, status=e.code)}"
            )
        raise SystemExit(f"fetch failed: {canonical} -> HTTP {e.code} {e.reason}.")

    # Detect the other common failure mode: a forge returned 200 OK with an
    # HTML login page instead of the file. Without this check, we'd silently
    # write HTML to the `.yaml` cache and pyyaml would choke later.
    if "html" in content_type.lower() and basename.endswith(".yaml"):
        raise SystemExit(
            f"fetch failed: {canonical} returned HTML (Content-Type: {content_type!r}, "
            f"final URL: {final_url}).\n"
            f"{_auth_hint(parsed, sent_token=sent_token, status=200)}"
        )

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
        f"  cg config providers --set              # interactive, saves to ~/.config/cg",
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
                f"`cg config providers --set`."
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
