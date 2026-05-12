"""Schema and preprocessing for `cg` openapi extension properties.

Users may write extension properties either in nested form:

    x-cg-header:
      default_volume:
        dst: ../out
        read_only: true

or in flattened form by concatenating segments with `-`:

    x-cg-header-default_volume-dst: ../out
    x-cg-header-default_volume-read_only: true

Both forms are equivalent. `preprocess(content)` recursively rewrites all
flat-form keys under any configured `PROP_PREFIXES` into the canonical nested
form, validates that no unknown properties appear, and errors out if both
forms set the same nested path.
"""

from __future__ import annotations

from typing import Any


# Type sentinel used in the schema tree below.
LEAF = "<leaf>"
OPEN_LEAF = "<open-leaf>"  # any value type allowed, no further descent


# Schema describing every property the codegen reads. Keys are property name
# segments (the part *after* the matched prefix). Nested mappings represent
# nested containers. `LEAF` marks a terminal property; `OPEN_LEAF` marks a
# leaf whose value may itself be a mapping that we don't validate further.
SCHEMA: dict[str, Any] = {
    "header": {
        "default_volume": {
            "dst": LEAF,
            "read_only": LEAF,
            "checks": LEAF,
            "imports": {
                "golang": LEAF,
                "python": LEAF,
            },
        },
    },
    "go-def": LEAF,
    "go-def-name": LEAF,
    "go-field-name": LEAF,
    "go-extra-tags": LEAF,
    "go-type-name-abbrev": LEAF,
    "go-null-or-undefined-is": LEAF,
    "enum-data-gen": LEAF,
}


def _matched_prefix(key: str, prefixes: list[str]) -> str | None:
    for p in prefixes:
        if key == p or key.startswith(p + "-"):
            return p
    return None


def _split_against_schema(suffix: str, schema: Any, full_key: str) -> list[str]:
    """Split `suffix` (e.g. `header-default_volume-dst`) into nested path
    segments according to `schema`. Errors out if the suffix doesn't match
    any known property path.

    The algorithm walks the suffix left-to-right consuming dash-delimited
    chunks; at each step it picks the longest chunk that matches a key in
    the current schema node, then descends.
    """
    parts = suffix.split("-")
    path: list[str] = []
    node = schema
    i = 0
    while i < len(parts):
        if not isinstance(node, dict):
            raise SystemExit(
                f"unknown property {full_key!r}: cannot descend past leaf "
                f"{'-'.join(path)!r}"
            )
        # Try the longest match first: chunk = parts[i:j], j ranges from
        # len(parts) down to i+1.
        found = None
        for j in range(len(parts), i, -1):
            candidate = "-".join(parts[i:j])
            if candidate in node:
                found = (candidate, j)
                break
        if found is None:
            raise SystemExit(
                f"unknown property {full_key!r}: no schema match for segment "
                f"starting at {parts[i]!r} under {'-'.join(path) or '<root>'}"
            )
        seg, next_i = found
        path.append(seg)
        node = node[seg]
        i = next_i
    return path


def _set_path(target: dict, path: list[str], value: Any, full_key: str) -> None:
    """Set `value` at nested `path` inside `target`. Errors if anything along
    the path is already populated (which means both flat and nested forms
    set the same property)."""
    cur = target
    for seg in path[:-1]:
        if seg in cur:
            existing = cur[seg]
            if not isinstance(existing, dict):
                raise SystemExit(
                    f"property {full_key!r} conflicts with existing value at "
                    f"{seg!r} (set by another form)"
                )
            cur = existing
        else:
            new_node: dict = {}
            cur[seg] = new_node
            cur = new_node
    leaf = path[-1]
    if leaf in cur:
        raise SystemExit(
            f"property {full_key!r} duplicates {leaf!r} already set by "
            f"another form"
        )
    cur[leaf] = value


def preprocess(content: Any, prefixes: list[str]) -> Any:
    """Recursively rewrite flat-form `<prefix>-<a>-<b>-...` keys into nested
    mappings, validating each against `SCHEMA`. Returns the same `content`
    object (mutated in place); non-mapping inputs are returned as-is."""
    if isinstance(content, list):
        for item in content:
            preprocess(item, prefixes)
        return content
    if not isinstance(content, dict):
        return content

    # Two-phase: first recurse into existing values, then flatten this level.
    # Recursing first means nested mappings we just wrote (from this level's
    # flattening) are already canonical when descended into next time.
    for v in content.values():
        preprocess(v, prefixes)

    flat_keys = [k for k in content if isinstance(k, str) and _matched_prefix(k, prefixes) is not None]
    if not flat_keys:
        return content

    for key in flat_keys:
        prefix = _matched_prefix(key, prefixes)
        assert prefix is not None
        if key == prefix:
            # Bare prefix as a property name (rare); ignore.
            continue
        suffix = key[len(prefix) + 1:]
        path = _split_against_schema(suffix, SCHEMA, key)
        # Canonical form: top-level key is `<prefix>-<first_segment>`, value
        # is a nested dict of the remaining segments. If `path` has length 1
        # the value is already at the canonical top-level location — only
        # rewrite when the user used flat form for deeper levels.
        canonical_top = f"{prefix}-{path[0]}"
        if len(path) == 1:
            # Already canonical (or single-segment leaf like `x-cg-go-def`).
            # If the canonical key differs from the original, it's a flat
            # alias of a single-segment property — but our schema's single-
            # segment entries map 1:1 with their full name, so `path[0]`
            # equals `suffix` here. No rewrite needed.
            continue

        value = content.pop(key)
        nested_path = path[1:]
        # Reuse the existing canonical top-level dict if present.
        if canonical_top in content:
            existing_top = content[canonical_top]
            if not isinstance(existing_top, dict):
                raise SystemExit(
                    f"property {key!r} conflicts with existing scalar at "
                    f"{canonical_top!r}"
                )
            top_node = existing_top
        else:
            top_node = {}
            content[canonical_top] = top_node
        _set_path(top_node, nested_path, value, key)

    # After flattening, descend again to validate that *literal* nested
    # mappings under each canonical top-level key only contain known fields.
    for key, value in content.items():
        if not isinstance(key, str):
            continue
        prefix = _matched_prefix(key, prefixes)
        if prefix is None or key == prefix:
            continue
        suffix = key[len(prefix) + 1:]
        if suffix not in SCHEMA:
            # Single-segment leaf like `go-def` — nothing to validate inside.
            continue
        subschema = SCHEMA[suffix]
        if isinstance(value, dict) and isinstance(subschema, dict):
            _validate_against_schema(value, subschema, key)

    return content


def _validate_against_schema(node: dict, schema: dict, parent_key: str) -> None:
    """Recursively check that every key in `node` is present in `schema`."""
    for k, v in node.items():
        if not isinstance(k, str):
            continue
        if k not in schema:
            raise SystemExit(
                f"unknown property {parent_key}.{k!r}: not in schema"
            )
        sub = schema[k]
        if isinstance(sub, dict) and isinstance(v, dict):
            _validate_against_schema(v, sub, f"{parent_key}.{k}")
