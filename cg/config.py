"""Persistent user config for `cg`.

Stores provider credentials at `~/.config/cg/openapi_providers.json`
(respects `XDG_CONFIG_HOME`). Schema:

    {
      "github": [
        {"domain": "github.com", "token": "ghp_..."}
      ],
      "gitlab": [
        {"domain": "gitlab.com", "token": "glpat-..."},
        {"domain": "gitlab.corp.example", "token": "glpat-..."}
      ]
    }

Tokens stored in this file feed into `remote._token_for` so URL fetches
pick them up automatically.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any


PROVIDERS = ("github", "gitlab")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "cg"
    return Path.home() / ".config" / "cg"


def providers_path() -> Path:
    return config_dir() / "openapi_providers.json"


def load_providers() -> dict[str, list[dict[str, str]]]:
    """Read the providers file. Returns an empty dict if absent."""
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
    out: dict[str, list[dict[str, str]]] = {}
    for provider, entries in data.items():
        if not isinstance(entries, list):
            raise SystemExit(f"{path}: {provider!r} must be a list")
        norm: list[dict[str, str]] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or "domain" not in entry or "token" not in entry:
                raise SystemExit(
                    f"{path}: {provider!r}[{i}] must be an object with 'domain' and 'token'"
                )
            norm.append({"domain": str(entry["domain"]), "token": str(entry["token"])})
        out[provider] = norm
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
    for entries in data.values():
        for entry in entries:
            if entry["domain"] == domain:
                return entry["token"]
    return None


def _mask(token: str) -> str:
    return "*" * 8


def render_masked(data: dict[str, list[dict[str, str]]]) -> str:
    """Render the providers file with tokens replaced by `********`."""
    masked: dict[str, Any] = {}
    for provider, entries in data.items():
        masked[provider] = [
            {"domain": e["domain"], "token": _mask(e["token"])} for e in entries
        ]
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
