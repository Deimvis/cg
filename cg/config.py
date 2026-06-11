"""Persistent user config for `cg`.

Stores provider credentials at `~/.config/cg/openapi_providers.json`
(respects `XDG_CONFIG_HOME`). Schema:

    {
      "mirrors": {
        "http": {
          "github.com":                "https://mynexus.example/repository/github-web",
          "raw.githubusercontent.com": "https://mynexus.example/repository/github-raw",
          "gitlab.com":                "https://mynexus.example/repository/gitlab-raw"
        },
        "custom": {
          "github-raw-via-nexus": {
            "match":   {"convertable": {"hostname": {"eq": "raw.githubusercontent.com"}}},
            "rewrite": {"url": {"template": "https://mynexus.example/repository/github-raw{path}"}}
          }
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

`mirrors.custom` is a more expressive section. Each entry has a free-form
unique name plus a `match` clause (exactly one of `exact` or `convertable`,
checked against the URL's hostname pre- and post-normalize respectively)
and a `rewrite.url.template` string using Python `str.format` placeholders
drawn from {schema, hostname, port, path, query}. Custom rules win over
the simple `http` map. The `convertable` form lets one rule catch both
the convenience URL (e.g. `github.com/.../blob/main/x`) and its
normalized raw form (`raw.githubusercontent.com/.../main/x`).

`fetch_mode` (gitlab entries only) selects how `/-/raw/...` URLs are
fetched: `"web"` keeps the plain URL, `"api"` rewrites it to
`/api/v4/projects/.../repository/files/.../raw?ref=...`. When unset,
the default is `"api"` for entries with a token (preserves historical
behavior) and `"web"` otherwise. `token` is optional when `fetch_mode`
is set.

`ip_resolution` (any provider) pins how DNS results for the entry's
domain are filtered when opening the outbound TCP connection. One of:
`"any"` (default — no filtering), `"prefer/ipv4"`, `"only/ipv4"`,
`"prefer/ipv6"`, `"only/ipv6"`. The lookup is keyed on the *actual
request host* (post-mirror, post-gitlab-API rewrite), so a mirror
host can be pinned independently of the original forge host. `token`
and `fetch_mode` are optional when `ip_resolution` is set.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any


PROVIDERS = ("github", "gitlab")
MIRRORS_KEY = "mirrors"
_KNOWN_MIRROR_PROTOCOLS = ("http", "custom")
_MIRROR_TEMPLATE_VARS = ("schema", "hostname", "port", "path", "query")
_MIRROR_MATCH_KINDS = ("exact", "convertable")
IP_RESOLUTION_VALUES = ("any", "prefer/ipv4", "only/ipv4", "prefer/ipv6", "only/ipv6")


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
            if "ip_resolution" in entry:
                ipr = entry["ip_resolution"]
                if ipr not in IP_RESOLUTION_VALUES:
                    raise SystemExit(
                        f"{path}: {provider!r}[{i}].ip_resolution must be one of "
                        f"{', '.join(IP_RESOLUTION_VALUES)}, got {ipr!r}"
                    )
                norm_entry["ip_resolution"] = ipr
            if (
                "token" not in norm_entry
                and "fetch_mode" not in norm_entry
                and "ip_resolution" not in norm_entry
            ):
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must set at least one of "
                    f"'token', 'fetch_mode', 'ip_resolution'"
                )
            if provider != "gitlab" and "token" not in norm_entry and "ip_resolution" not in norm_entry:
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must set 'token'"
                )
            norm.append(norm_entry)
        out[provider] = norm
    return out


def _load_mirrors(value: Any, path: Path) -> dict[str, Any]:
    """Validate and normalize the `mirrors` top-level block."""
    from . import remote
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: 'mirrors' must be an object")
    out: dict[str, Any] = {}
    for proto, body in value.items():
        if proto not in _KNOWN_MIRROR_PROTOCOLS:
            raise SystemExit(
                f"{path}: 'mirrors.{proto}' is not a recognized protocol; "
                f"expected one of: {', '.join(_KNOWN_MIRROR_PROTOCOLS)}"
            )
        if proto == "custom":
            out[proto] = _load_custom_mirrors(body, path)
            continue
        if not isinstance(body, dict):
            raise SystemExit(f"{path}: 'mirrors.{proto}' must be an object")
        norm: dict[str, str] = {}
        for host, prefix in body.items():
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


def _load_custom_mirrors(value: Any, path: Path) -> list[dict[str, Any]]:
    """Validate and normalize `mirrors.custom`. Returns a list of rules in
    JSON declaration order: [{'name', 'match', 'rewrite'}, ...]."""
    from . import remote
    import urllib.parse
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: 'mirrors.custom' must be an object")
    rules: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name, rule in value.items():
        loc = f"'mirrors.custom[{name!r}]'"
        if name in seen_names:
            raise SystemExit(f"{path}: {loc}: duplicate rule name")
        seen_names.add(name)
        if not isinstance(rule, dict):
            raise SystemExit(f"{path}: {loc} must be an object")
        extra = set(rule.keys()) - {"match", "rewrite"}
        if extra:
            raise SystemExit(
                f"{path}: {loc}: unexpected keys {sorted(extra)}; allowed: match, rewrite"
            )
        if "match" not in rule or "rewrite" not in rule:
            raise SystemExit(f"{path}: {loc}: must set both 'match' and 'rewrite'")
        match = rule["match"]
        if not isinstance(match, dict):
            raise SystemExit(f"{path}: {loc}.match must be an object")
        present = [k for k in _MIRROR_MATCH_KINDS if k in match]
        unknown = set(match.keys()) - set(_MIRROR_MATCH_KINDS)
        if unknown:
            raise SystemExit(
                f"{path}: {loc}.match: unexpected keys {sorted(unknown)}; "
                f"allowed: {', '.join(_MIRROR_MATCH_KINDS)}"
            )
        if len(present) != 1:
            raise SystemExit(
                f"{path}: {loc}.match: must set exactly one of "
                f"{', '.join(_MIRROR_MATCH_KINDS)}, got {present!r}"
            )
        kind = present[0]
        spec = match[kind]
        if not isinstance(spec, dict) or set(spec.keys()) != {"hostname"}:
            raise SystemExit(
                f"{path}: {loc}.match.{kind}: must be an object with a single 'hostname' key"
            )
        hostname_pred = spec["hostname"]
        if (
            not isinstance(hostname_pred, dict)
            or set(hostname_pred.keys()) != {"eq"}
            or not isinstance(hostname_pred.get("eq"), str)
            or not hostname_pred["eq"]
        ):
            raise SystemExit(
                f"{path}: {loc}.match.{kind}.hostname: must be {{'eq': <non-empty string>}}"
            )
        eq_value = hostname_pred["eq"]

        rewrite = rule["rewrite"]
        if not isinstance(rewrite, dict) or set(rewrite.keys()) != {"url"}:
            raise SystemExit(
                f"{path}: {loc}.rewrite: must be an object with a single 'url' key"
            )
        url_spec = rewrite["url"]
        if not isinstance(url_spec, dict) or set(url_spec.keys()) != {"template"}:
            raise SystemExit(
                f"{path}: {loc}.rewrite.url: must be an object with a single 'template' key"
            )
        template = url_spec["template"]
        if not isinstance(template, str) or not template:
            raise SystemExit(
                f"{path}: {loc}.rewrite.url.template: must be a non-empty string"
            )
        try:
            sample = {v: "" for v in _MIRROR_TEMPLATE_VARS}
            template.format_map(sample)
        except KeyError as e:
            raise SystemExit(
                f"{path}: {loc}.rewrite.url.template: unknown placeholder {{{e.args[0]}}}; "
                f"available: {', '.join(_MIRROR_TEMPLATE_VARS)}"
            )
        except (IndexError, ValueError) as e:
            raise SystemExit(
                f"{path}: {loc}.rewrite.url.template: invalid format string: {e}"
            )

        probe_vars = {
            "schema":   "https",
            "hostname": eq_value,
            "port":     "",
            "path":     "/probe",
            "query":    "",
        }
        try:
            probe = template.format_map(probe_vars)
        except Exception as e:
            raise SystemExit(
                f"{path}: {loc}.rewrite.url.template: failed to expand: {e}"
            )
        if not remote.is_url(probe):
            raise SystemExit(
                f"{path}: {loc}.rewrite.url.template: must produce an http(s) URL when expanded, got {probe!r}"
            )
        if urllib.parse.urlparse(probe).netloc.split("@")[-1].split(":")[0] == eq_value:
            raise SystemExit(
                f"{path}: {loc}: rewrite host equals match hostname {eq_value!r} (would loop)"
            )

        rules.append({
            "name": str(name),
            "match": {kind: {"hostname": {"eq": eq_value}}},
            "rewrite": {"url": {"template": template}},
        })
    return rules


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


DEFAULTS_FILENAME = "defaults.json"

SYSTEM_DEFAULTS: dict[str, Any] = {
    "cache": {"enabled": False, "ttl": {"d": 7}},
}

_DURATION_KEYS = ("d", "h", "m", "s", "ms", "mcs", "ns")

_defaults_path_override: Path | None = None


def set_defaults_path_override(p: Path | None) -> Path | None:
    """Redirect `defaults_path()` to `p` for the rest of this process. Returns
    the previous override so callers can restore it in a `finally`. Only the
    transient `cg with` machinery should touch this — persistent commands
    must keep targeting the real on-disk file."""
    global _defaults_path_override
    prev = _defaults_path_override
    _defaults_path_override = p
    return prev


def defaults_path() -> Path:
    if _defaults_path_override is not None:
        return _defaults_path_override
    return config_dir() / DEFAULTS_FILENAME


def _validate_defaults(data: Any, path: Path) -> None:
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top-level must be an object")
    unknown = set(data.keys()) - set(SYSTEM_DEFAULTS.keys())
    if unknown:
        raise SystemExit(
            f"{path}: unknown key(s) {sorted(unknown)!r}; "
            f"allowed: {sorted(SYSTEM_DEFAULTS.keys())!r}"
        )
    if "cache" in data:
        _validate_cache(data["cache"], path)


def _validate_cache(value: Any, path: Path) -> None:
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: 'cache' must be an object")
    allowed = {"enabled", "ttl"}
    unknown = set(value.keys()) - allowed
    if unknown:
        raise SystemExit(
            f"{path}: unknown key(s) under 'cache': {sorted(unknown)!r}; "
            f"allowed: {sorted(allowed)!r}"
        )
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise SystemExit(
            f"{path}: 'cache.enabled' must be a boolean, got "
            f"{type(value['enabled']).__name__}"
        )
    if "ttl" in value:
        _validate_duration(value["ttl"], path, "cache.ttl")


def _validate_duration(value: Any, path: Path, loc: str) -> None:
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: {loc!r} must be an object")
    unknown = set(value.keys()) - set(_DURATION_KEYS)
    if unknown:
        raise SystemExit(
            f"{path}: unknown key(s) under {loc!r}: {sorted(unknown)!r}; "
            f"allowed: {list(_DURATION_KEYS)!r}"
        )
    if not value:
        raise SystemExit(f"{path}: {loc!r} must set at least one component")
    total = 0
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, int):
            raise SystemExit(
                f"{path}: {loc}.{k} must be a non-negative integer, got "
                f"{type(v).__name__}"
            )
        if v < 0:
            raise SystemExit(f"{path}: {loc}.{k} must be >= 0, got {v}")
        total += v
    if total == 0:
        raise SystemExit(f"{path}: {loc!r} total duration must be > 0")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: `overlay` recursively merged onto `base`. When both
    sides have a dict at the same key, recurse; otherwise the overlay value
    wins (and a non-dict overlay replaces a dict base outright)."""
    out: dict[str, Any] = {}
    for k, v in base.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = dict(v) if isinstance(v, dict) else v
    return out


def load_defaults() -> dict[str, Any]:
    """Read the defaults file. Returns `{}` if absent. Validates strictly."""
    path = defaults_path()
    if not path.exists():
        return {}
    try:
        with path.open("r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path}: invalid JSON: {e}")
    _validate_defaults(data, path)
    return data


def save_defaults(data: dict[str, Any]) -> Path:
    path = defaults_path()
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


def effective_defaults() -> dict[str, Any]:
    """User file deep-merged onto SYSTEM_DEFAULTS. User keys win."""
    return _deep_merge(SYSTEM_DEFAULTS, load_defaults())


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


def ip_resolution_for_domain(domain: str) -> str:
    """Return the configured ip_resolution for `domain`, or 'any' if unset.

    Scans all provider entries (github, gitlab, ...); the flag is a
    network-layer concern that applies regardless of provider.
    """
    data = load_providers()
    for key, entries in data.items():
        if key == MIRRORS_KEY:
            continue
        for entry in entries:
            if entry["domain"] == domain:
                return entry.get("ip_resolution", "any")
    return "any"


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


def custom_mirrors() -> list[dict[str, Any]]:
    """Return configured custom mirror rules in declaration order.

    Each item: {'name': str, 'match': {<kind>: {'hostname': {'eq': str}}},
    'rewrite': {'url': {'template': str}}} where <kind> is 'exact' or
    'convertable'.
    """
    data = load_providers()
    mirrors = data.get(MIRRORS_KEY)
    if not isinstance(mirrors, dict):
        return []
    rules = mirrors.get("custom")
    return rules if isinstance(rules, list) else []


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


PATCH_TYPES = ("kv", "patch-file")


def _parse_kv_scalar(raw: str) -> Any:
    """Coerce a bare (un-braced) RHS token into bool/int/string."""
    s = raw.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        return int(s)
    except ValueError:
        return s


def _split_top_level(body: str, sep: str) -> list[str]:
    """Split `body` on `sep` chars that are at brace-depth 0. Used to break
    an object body `a:1,b:{c:2,d:3}` into [`a:1`, `b:{c:2,d:3}`]."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise SystemExit(f"unmatched '}}' in {body!r}")
        elif ch == sep and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    if depth != 0:
        raise SystemExit(f"unmatched '{{' in {body!r}")
    parts.append(body[start:])
    return parts


def _parse_kv_value(raw: str, arg: str) -> Any:
    """Parse the RHS of a patch arg. Supports bool / int / string scalars and
    braced objects `{k:v,k2:v2}` (recursive). `arg` is the original full
    argument, used only for error messages."""
    s = raw.strip()
    if not s.startswith("{"):
        return _parse_kv_scalar(s)
    if not s.endswith("}"):
        raise SystemExit(f"{arg!r}: braced value must end with '}}'")
    body = s[1:-1].strip()
    if not body:
        raise SystemExit(f"{arg!r}: empty object value is not allowed")
    out: dict[str, Any] = {}
    for piece in _split_top_level(body, ","):
        piece = piece.strip()
        if not piece:
            raise SystemExit(f"{arg!r}: empty entry in object value")
        if ":" not in piece:
            raise SystemExit(
                f"{arg!r}: missing ':' inside object value (got {piece!r}); "
                f"use 'key:value' inside braces"
            )
        depth = 0
        colon_idx = -1
        for i, ch in enumerate(piece):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ":" and depth == 0:
                colon_idx = i
                break
        if colon_idx < 0:
            raise SystemExit(
                f"{arg!r}: missing top-level ':' inside object value piece {piece!r}"
            )
        key = piece[:colon_idx].strip()
        val_raw = piece[colon_idx + 1:].strip()
        if not key:
            raise SystemExit(f"{arg!r}: empty key in object value")
        if key in out:
            raise SystemExit(f"{arg!r}: duplicate key {key!r} in object value")
        out[key] = _parse_kv_value(val_raw, arg)
    return out


def _parse_kv_arg(arg: str) -> tuple[list[str], Any]:
    """Parse one positional patch arg of the form `<dotted.path>=<value>`."""
    if "=" not in arg:
        raise SystemExit(
            f"{arg!r}: expected '<dotted.path>=<value>' (missing '=')"
        )
        # NB: a braced value may itself contain `=`-free content; we split on
        # the first '=' before any '{' so this is unambiguous.
    eq_idx = arg.find("=")
    brace_idx = arg.find("{")
    if 0 <= brace_idx < eq_idx:
        raise SystemExit(
            f"{arg!r}: '=' must come before '{{' (no '=' found in dotted-path part)"
        )
    path_raw = arg[:eq_idx]
    value_raw = arg[eq_idx + 1:]
    if not path_raw:
        raise SystemExit(f"{arg!r}: empty dotted path before '='")
    parts = path_raw.split(".")
    if any(not p for p in parts):
        raise SystemExit(f"{arg!r}: empty segment in dotted path {path_raw!r}")
    return parts, _parse_kv_value(value_raw, arg)


def _patch_set(target: dict[str, Any], path: list[str], value: Any) -> None:
    """Set `value` at nested `path` inside `target`, replacing whatever is
    there (including a dict)."""
    cur = target
    for seg in path[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[path[-1]] = value


def cmd_defaults_show() -> int:
    """`cg config defaults` — print the effective view (system defaults merged
    with the user file, if any)."""
    print(f"# {defaults_path()} (effective view, system defaults merged)")
    print(json.dumps(effective_defaults(), indent=2))
    return 0


def cmd_defaults_init() -> int:
    path = defaults_path()
    if path.exists():
        print(f"defaults config already exists at {path}")
        return 0
    save_defaults(SYSTEM_DEFAULTS)
    print(f"created {path}")
    return 0


def cmd_defaults_reset() -> int:
    path = defaults_path()
    existed = path.exists()
    save_defaults(SYSTEM_DEFAULTS)
    print(f"{'overwrote' if existed else 'created'} {path}")
    return 0


def apply_patch_inputs(
    current: dict[str, Any], patch_type: str, inputs: list[str]
) -> dict[str, Any]:
    """Apply `cg config defaults patch` semantics to `current` and return the
    new data. Pure: does not read or write the filesystem. Validates inputs
    and (in patch-file mode) errors out cleanly."""
    if patch_type == "patch-file":
        raise SystemExit(
            "cg config defaults patch -t patch-file: not yet implemented"
        )
    if patch_type != "kv":
        raise SystemExit(
            f"cg config defaults patch: unknown -t value {patch_type!r}; "
            f"expected one of: {', '.join(PATCH_TYPES)}"
        )
    if not inputs:
        raise SystemExit(
            "cg config defaults patch: at least one <dotted.path>=<value> "
            "argument required"
        )
    merged = _deep_merge({}, current)
    for raw in inputs:
        path_parts, value = _parse_kv_arg(raw)
        _patch_set(merged, path_parts, value)
    return merged


def cmd_defaults_patch(patch_type: str, inputs: list[str]) -> int:
    merged = apply_patch_inputs(load_defaults(), patch_type, inputs)
    _validate_defaults(merged, defaults_path())
    out_path = save_defaults(merged)
    print(f"patched {out_path}")
    return 0
