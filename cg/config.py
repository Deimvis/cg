"""Persistent user config for `cg`.

Stores provider credentials at `~/.config/cg/openapi_providers.json`
(respects `XDG_CONFIG_HOME`). Schema:

    {
      "mirrors": {
        "http": {
          "github.com":                "https://mynexus.example/repository/github-web",
          "raw.githubusercontent.com": "https://mynexus.example/repository/github-raw",
          "gitlab.com":                "https://mynexus.example/repository/gitlab-raw"
        }
      },
      "github": [
        {"domain": "github.com", "token": "ghp_..."}
      ],
      "gitlab": [
        {"domain": "gitlab.com", "token": "glpat-..."},
        {"domain": "gitlab.corp.example", "token": "glpat-...", "fetch_mode": "web"},
        {"domain": "gitlab.public.example", "fetch_mode": "web"}
      ]
    }

Tokens feed into `remote._token_for` so URL fetches pick them up
automatically. HTTP mirrors feed into `remote._apply_http_mirror` and
rewrite outbound URLs from the configured host to the corresponding
prefix while still authenticating against the original host's token.

`fetch_mode` (gitlab entries only) selects how `/-/raw/...` URLs are
fetched: `"web"` keeps the plain URL, `"api"` rewrites it to
`/api/v4/projects/.../repository/files/.../raw?ref=...`. When unset,
the default is `"api"` for entries with a token (preserves historical
behavior) and `"web"` otherwise. `token` is optional when `fetch_mode`
is set.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any


PROVIDERS = ("github", "gitlab")
MIRRORS_KEY = "mirrors"
_KNOWN_MIRROR_PROTOCOLS = ("http",)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "cg"
    return Path.home() / ".config" / "cg"


def providers_path() -> Path:
    return config_dir() / "openapi_providers.json"


def load_providers() -> dict[str, Any]:
    """Read the providers file. Returns an empty dict if absent.

    Two known top-level shapes:
      - `<provider>`: list of `{domain, token}` entries.
      - `mirrors`: dict with optional `http: {host: prefix-url}`.
    """
    path = providers_path()
    if not path.exists():
        return {}
    try:
        with path.open("r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path}: invalid JSON: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top-level must be an object")
    out: dict[str, Any] = {}
    for provider, entries in data.items():
        if provider == MIRRORS_KEY:
            out[provider] = _load_mirrors(entries, path)
            continue
        if not isinstance(entries, list):
            raise SystemExit(f"{path}: {provider!r} must be a list")
        norm: list[dict[str, str]] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or "domain" not in entry:
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must be an object with 'domain'"
                )
            norm_entry: dict[str, str] = {"domain": str(entry["domain"])}
            if "token" in entry:
                norm_entry["token"] = str(entry["token"])
            if provider == "gitlab" and "fetch_mode" in entry:
                mode = entry["fetch_mode"]
                if mode not in ("web", "api"):
                    raise SystemExit(
                        f"{path}: {provider!r}[{i}].fetch_mode must be 'web' or 'api', got {mode!r}"
                    )
                norm_entry["fetch_mode"] = mode
            if "token" not in norm_entry and "fetch_mode" not in norm_entry:
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must set at least 'token' or 'fetch_mode'"
                )
            if provider != "gitlab" and "token" not in norm_entry:
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must set 'token'"
                )
            norm.append(norm_entry)
        out[provider] = norm
    return out


def _load_mirrors(value: Any, path: Path) -> dict[str, dict[str, str]]:
    """Validate and normalize the `mirrors` top-level block."""
    from . import remote
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: 'mirrors' must be an object")
    out: dict[str, dict[str, str]] = {}
    for proto, hosts in value.items():
        if proto not in _KNOWN_MIRROR_PROTOCOLS:
            raise SystemExit(
                f"{path}: 'mirrors.{proto}' is not a recognized protocol; "
                f"expected one of: {', '.join(_KNOWN_MIRROR_PROTOCOLS)}"
            )
        if not isinstance(hosts, dict):
            raise SystemExit(f"{path}: 'mirrors.{proto}' must be an object")
        norm: dict[str, str] = {}
        for host, prefix in hosts.items():
            if not isinstance(prefix, str) or not remote.is_url(prefix):
                raise SystemExit(
                    f"{path}: 'mirrors.{proto}[{host!r}]' must be an http(s) URL string"
                )
            import urllib.parse
            prefix_parsed = urllib.parse.urlparse(prefix)
            if prefix_parsed.netloc == host:
                raise SystemExit(
                    f"{path}: 'mirrors.{proto}[{host!r}]' prefix host equals "
                    f"original host (would loop)"
                )
            norm[str(host)] = prefix.rstrip("/")
        out[proto] = norm
    return out


def save_providers(data: dict[str, list[dict[str, str]]]) -> Path:
    path = providers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def token_for_domain(domain: str) -> str | None:
    """Return the configured token for `domain`, or None if none is set."""
    data = load_providers()
    for key, entries in data.items():
        if key == MIRRORS_KEY:
            continue
        for entry in entries:
            if entry["domain"] == domain:
                return entry.get("token")
    return None


def gitlab_fetch_mode(domain: str) -> str:
    """Return 'web' or 'api' for a gitlab domain.

    Defaults: 'api' if the entry has a token (preserves historical behavior),
    'web' otherwise (no entry, or entry without a token).
    """
    data = load_providers()
    for entry in data.get("gitlab", []):
        if entry["domain"] == domain:
            if "fetch_mode" in entry:
                return entry["fetch_mode"]
            return "api" if "token" in entry else "web"
    return "web"


def http_mirror_for(host: str) -> str | None:
    """Return the configured http mirror prefix for `host`, or None."""
    data = load_providers()
    mirrors = data.get(MIRRORS_KEY)
    if not isinstance(mirrors, dict):
        return None
    http = mirrors.get("http")
    if not isinstance(http, dict):
        return None
    prefix = http.get(host)
    return prefix if isinstance(prefix, str) else None


def _mask(token: str) -> str:
    return "*" * 8


def render_masked(data: dict[str, Any]) -> str:
    """Render the providers file with tokens replaced by `********`.
    Mirror prefixes are shown unmasked (they're not secrets)."""
    masked: dict[str, Any] = {}
    for key, value in data.items():
        if key == MIRRORS_KEY:
            masked[key] = value
            continue
        entries = []
        for e in value:
            me = dict(e)
            if "token" in me:
                me["token"] = _mask(me["token"])
            entries.append(me)
        masked[key] = entries
    return json.dumps(masked, indent=2) + "\n"


def cmd_list() -> int:
    data = load_providers()
    if not data:
        print(f"(no providers configured at {providers_path()})")
        return 0
    print(f"# {providers_path()}")
    print(render_masked(data), end="")
    return 0


def _prompt(label: str, choices: tuple[str, ...] | None = None) -> str:
    suffix = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        val = input(f"{label}{suffix}: ").strip()
        if choices is not None and val not in choices:
            print(f"  expected one of: {', '.join(choices)}")
            continue
        if not val:
            print("  value required")
            continue
        return val


def _upsert(provider: str, domain: str, token: str) -> tuple[Path, bool]:
    data = load_providers()
    entries = data.setdefault(provider, [])
    if not isinstance(entries, list):
        raise SystemExit(
            f"{providers_path()}: {provider!r} must be a list, got {type(entries).__name__}"
        )
    updated = False
    for entry in entries:
        if entry["domain"] == domain:
            entry["token"] = token
            updated = True
            break
    if not updated:
        entries.append({"domain": domain, "token": token})
    return save_providers(data), updated


def cmd_add(provider: str | None, domain: str | None, token: str | None) -> int:
    """Add or update a provider. All three args present → non-interactive.
    All three absent → interactive prompts. Any other partial combination is
    a usage error."""
    supplied = [v for v in (provider, domain, token) if v is not None]
    if len(supplied) not in (0, 3):
        raise SystemExit(
            "cg config providers add: pass either no positional args (interactive) "
            "or all three: <provider> <domain> <token>"
        )

    if not supplied:
        provider = _prompt("Provider", PROVIDERS)
        default_domain = "github.com" if provider == "github" else "gitlab.com"
        raw_domain = input(f"Domain [{default_domain}]: ").strip()
        domain = raw_domain or default_domain
        token = getpass.getpass("Token (hidden): ").strip()
        if not token:
            raise SystemExit("token required")
    else:
        assert provider is not None and domain is not None and token is not None
        if provider not in PROVIDERS:
            raise SystemExit(
                f"unknown provider {provider!r}; expected one of: {', '.join(PROVIDERS)}"
            )
        if not domain:
            raise SystemExit("domain must be non-empty")
        if not token:
            raise SystemExit("token must be non-empty")

    path, updated = _upsert(provider, domain, token)
    action = "updated" if updated else "added"
    print(f"{action} {provider}/{domain} in {path}")
    return 0
