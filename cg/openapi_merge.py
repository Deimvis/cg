"""Standalone OpenAPI bundler ("swagger-merger" equivalent).

Produces a single self-contained OpenAPI 3.x YAML by resolving every external
`$ref` via cg's existing remote-aware resolver (so authenticated GitLab/GitHub
refs and `main`/`master` fallback work for free).

Two implementations:
  - `preserved-schemas` (default): hoist external components into the root
    `components/*`, keep original names, rewrite refs to same-doc pointers.
    On name collision, disambiguate with the immediate parent dir name as a
    `<parent>_<name>` prefix. Identical content is deduplicated.
  - `inlined-schemas`: match swagger-merger — no hoisting; replace each
    external `$ref` with the resolved content at the use site.

Same-file refs (`$ref: '#/components/schemas/X'`) are left untouched in both
modes — they already point inside the bundled document.
"""

from __future__ import annotations

import copy
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from . import openapi_lib, remote


IMPL_PRESERVED_SCHEMAS = "preserved-schemas"
IMPL_INLINED_SCHEMAS = "inlined-schemas"
IMPLS = [IMPL_PRESERVED_SCHEMAS, IMPL_INLINED_SCHEMAS]
DEFAULT_IMPL = IMPL_PRESERVED_SCHEMAS


def run(input_file: Path, output_file: Path, impl: str) -> None:
    root = openapi_lib.load_yaml(input_file)
    if not isinstance(root, dict):
        raise SystemExit(f"openapi/standalone: input is not a YAML mapping: {input_file}")

    if root.get("swagger") == "2.0" or not str(root.get("openapi", "")).startswith("3."):
        raise SystemExit(
            "openapi/standalone requires OpenAPI 3.x input "
            f"(got {('swagger=' + str(root.get('swagger'))) if root.get('swagger') else ('openapi=' + str(root.get('openapi')))})"
        )

    merged = copy.deepcopy(root)

    if impl == IMPL_PRESERVED_SCHEMAS:
        _run_preserved(merged, input_file)
    elif impl == IMPL_INLINED_SCHEMAS:
        _run_inlined(merged, input_file)
    else:
        raise SystemExit(f"openapi/standalone: unknown impl {impl!r}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w") as f:
        yaml.dump(
            merged,
            f,
            Dumper=_IndentedDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            indent=2,
        )


class _IndentedDumper(yaml.SafeDumper):
    """SafeDumper that indents list dashes under their parent key instead of
    rendering them flush-left (PyYAML's default "indentless" sequence style)."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # type: ignore[override]
        return super().increase_indent(flow, False)


def _source_label(fp: Path) -> str:
    url = remote.url_of_cache_path(fp)
    return url if url is not None else str(fp)


# ---------- preserved-schemas ----------

def _run_preserved(merged: dict, input_file: Path) -> None:
    components_index: dict[tuple[str, str], tuple[Path, Any]] = {}
    seed_components = merged.get("components")
    if isinstance(seed_components, dict):
        for kind, entries in seed_components.items():
            if not isinstance(entries, dict):
                continue
            for name, value in entries.items():
                components_index[(kind, name)] = (input_file, value)

    ref_rewrites: dict[tuple[Path, str], str] = {}
    pending: deque[tuple[Path, Any]] = deque()

    # Pre-seed pending with the root file's own components so their inner refs
    # are walked exactly once with `input_file` as the source — and so the root
    # walk below can safely skip `merged["components"]` (otherwise it would
    # re-walk newly hoisted nodes against the wrong source file).
    if isinstance(seed_components, dict):
        for entries in seed_components.values():
            if isinstance(entries, dict):
                for value in entries.values():
                    pending.append((input_file, value))

    def hoist(source_file: Path, ref_value: str) -> str:
        memo_key = (source_file, ref_value)
        if memo_key in ref_rewrites:
            return ref_rewrites[memo_key]

        target, def_path = openapi_lib._parse_ref(ref_value)
        ref_fp = openapi_lib._resolve_ref_target(source_file, target)
        resolved = openapi_lib.get_def_schema(ref_fp, def_path)

        if len(def_path) >= 2 and def_path[0] == "components":
            kind = def_path[-2]
            original_name = def_path[-1]
        else:
            kind = "schemas"
            original_name = def_path[-1]

        final_name = _assign_name(components_index, kind, original_name, ref_fp, resolved)

        components = merged.setdefault("components", {})
        bucket = components.setdefault(kind, {})
        if final_name not in bucket:
            bucket[final_name] = copy.deepcopy(resolved)
            components_index[(kind, final_name)] = (ref_fp, resolved)
            pending.append((ref_fp, bucket[final_name]))

        new_pointer = f"#/components/{kind}/{final_name}"
        ref_rewrites[memo_key] = new_pointer
        return new_pointer

    def walk(node: Any, source_file: Path, skip_components: bool = False) -> None:
        if isinstance(node, dict):
            ref_value = node.get("$ref")
            if isinstance(ref_value, str):
                target, _ = openapi_lib._parse_ref(ref_value)
                # A same-doc ref (`target == ""`) is fine when we are walking
                # the root document, but inside a hoisted external schema it
                # points into *that* schema's source file — the referenced
                # component lives there, not in the output's components. Hoist
                # it into the output so the pointer is valid.
                if target == "" and source_file == input_file:
                    return
                node["$ref"] = hoist(source_file, ref_value)
                # Walk siblings too: $ref with adjacent keywords is valid in
                # OpenAPI 3.1 / JSON Schema 2020-12 and those siblings may
                # themselves contain refs.
                for k, v in list(node.items()):
                    if k == "$ref":
                        continue
                    walk(v, source_file)
                return
            for k, v in list(node.items()):
                if skip_components and k == "components":
                    continue
                walk(v, source_file)
        elif isinstance(node, list):
            for item in list(node):
                walk(item, source_file)

    walk(merged, input_file, skip_components=True)
    while pending:
        src, node = pending.popleft()
        walk(node, src)


def _assign_name(
    components_index: dict[tuple[str, str], tuple[Path, Any]],
    kind: str,
    original_name: str,
    ref_fp: Path,
    resolved: Any,
) -> str:
    existing = components_index.get((kind, original_name))
    if existing is None:
        return original_name
    if existing[1] == resolved:
        return original_name

    candidate = f"{ref_fp.parent.name}_{original_name}"
    clash = components_index.get((kind, candidate))
    if clash is None:
        return candidate
    if clash[1] == resolved:
        return candidate

    raise SystemExit(
        f"openapi/standalone: name collision for components.{kind}.{original_name} between "
        f"{_source_label(existing[0])} and {_source_label(ref_fp)}; "
        f"candidate {candidate!r} also collides with {_source_label(clash[0])}. "
        "Rename one of the conflicting definitions or move its file under a "
        "distinct parent directory."
    )


# ---------- inlined-schemas ----------

def _run_inlined(merged: dict, input_file: Path) -> None:
    stack: list[tuple[Path, tuple[str, ...]]] = []

    def resolve_or_die(ref_fp: Path, def_path: list[str], source_file: Path, ref_value: str) -> Any:
        try:
            return openapi_lib.get_def_schema(ref_fp, def_path)
        except (KeyError, TypeError):
            raise SystemExit(
                f"openapi/standalone (inlined-schemas): cannot resolve $ref {ref_value!r} "
                f"from {_source_label(source_file)} -> {_source_label(ref_fp)}; "
                f"path /{'/'.join(def_path)} not found"
            )

    def inline_into(node: dict, source_file: Path, ref_value: str) -> Path:
        """Inline the `$ref` declared in `node` in place: remove the `$ref` key
        and merge the resolved value into `node`. Sibling keys win on conflict
        (matches OpenAPI 3.1 / JSON Schema 2020-12 semantics for `$ref` with
        adjacent keywords). Returns the source file of the resolved value so
        the caller can keep walking with the right base path."""
        target, def_path = openapi_lib._parse_ref(ref_value)
        # Same-doc refs (`target == ""`) resolve back to the current source
        # file: when an inlined external schema contains `$ref: '#/...X'`, X
        # lives in the file the schema came from — not in the root output.
        ref_fp = openapi_lib._resolve_ref_target(source_file, target)
        frame = (ref_fp, tuple(def_path))
        if frame in stack:
            cycle = " -> ".join(f"{_source_label(fp)}#/{'/'.join(p)}" for fp, p in stack + [frame])
            raise SystemExit(f"openapi/standalone: ref cycle detected: {cycle}")
        resolved = resolve_or_die(ref_fp, def_path, source_file, ref_value)
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        if not isinstance(resolved, dict):
            if siblings:
                raise SystemExit(
                    f"openapi/standalone (inlined-schemas): cannot merge $ref {ref_value!r} "
                    f"whose resolved value is not a mapping ({type(resolved).__name__}) "
                    f"with sibling keys {sorted(siblings)!r}"
                )
            # No siblings: the parent will replace this whole node. We can't
            # do that from here without the parent reference; punt to caller.
            return ref_fp  # type: ignore[return-value]
        node.clear()
        node.update(copy.deepcopy(resolved))
        # Sibling keys win on conflict.
        node.update(siblings)
        return ref_fp

    def walk(node: Any, source_file: Path, parent: Any = None, parent_key: Any = None) -> None:
        if isinstance(node, dict):
            ref_value = node.get("$ref")
            if isinstance(ref_value, str):
                if parent is None:
                    raise SystemExit(
                        "openapi/standalone: refusing to inline a $ref at the document root"
                    )
                target, def_path = openapi_lib._parse_ref(ref_value)
                ref_fp = openapi_lib._resolve_ref_target(source_file, target)
                frame = (ref_fp, tuple(def_path))
                resolved = resolve_or_die(ref_fp, def_path, source_file, ref_value)
                if not isinstance(resolved, dict):
                    # Scalar/list target with no siblings: replace the whole
                    # parent slot. With siblings, error.
                    siblings = {k: v for k, v in node.items() if k != "$ref"}
                    if siblings:
                        raise SystemExit(
                            f"openapi/standalone (inlined-schemas): cannot merge $ref {ref_value!r} "
                            f"whose resolved value is not a mapping ({type(resolved).__name__}) "
                            f"with sibling keys {sorted(siblings)!r}"
                        )
                    if frame in stack:
                        cycle = " -> ".join(f"{_source_label(fp)}#/{'/'.join(p)}" for fp, p in stack + [frame])
                        raise SystemExit(f"openapi/standalone: ref cycle detected: {cycle}")
                    parent[parent_key] = copy.deepcopy(resolved)
                    return
                # Dict target: inline into the same node, preserving siblings.
                inline_into(node, source_file, ref_value)
                stack.append(frame)
                try:
                    walk(node, ref_fp, parent, parent_key)
                finally:
                    stack.pop()
                return
            for k, v in list(node.items()):
                walk(v, source_file, node, k)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, source_file, node, i)

    walk(merged, input_file)
