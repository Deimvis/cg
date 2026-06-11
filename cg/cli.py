import argparse
from pathlib import Path

import sys

from . import config, openapi_go, openapi_lib, openapi_merge, remote, sql_tests_go


SRC_OPENAPI = "openapi"
SRC_SQL = "sql"

DST_GO = "go"
DST_GO_TESTS = "go/tests"
DST_OPENAPI_STANDALONE = "openapi/standalone"


# Allowed --impl values per (src_type, dst_type). First entry is the default.
# Pairs absent from this map don't accept --impl.
IMPLS_BY_PAIR: dict[tuple[str, str], list[str]] = {
    (SRC_SQL, DST_GO_TESTS): sql_tests_go.IMPLS,
    (SRC_OPENAPI, DST_OPENAPI_STANDALONE): openapi_merge.IMPLS,
}


def _parse_volumes(raw: list[str] | None) -> list[openapi_lib.Volume]:
    if not raw:
        return []
    return [
        openapi_lib.Volume(**remote.parse_volume(v, render_dst=openapi_lib.render_dst))
        for v in raw
    ]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cg", description="codegen entrypoint")
    p.add_argument("--src-type", required=True, choices=[SRC_OPENAPI, SRC_SQL])
    p.add_argument("--dst-type", required=True, choices=[DST_GO, DST_GO_TESTS, DST_OPENAPI_STANDALONE])
    p.add_argument(
        "input",
        type=str,
        help=(
            "Input. openapi: file path, http(s) URL, or a glob suffix "
            "('<prefix>/*' for one directory, '<prefix>/**' for recursive — "
            "quote it so the shell does not expand it). sql: directory path"
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file or directory (required for sql; optional for openapi)",
    )
    p.add_argument(
        "-v",
        "--volume",
        type=str,
        action="append",
        default=[],
        help="Volume in form `src:dst` for openapi codegen (repeat for multiple)",
    )
    p.add_argument(
        "-e",
        "--extra",
        type=str,
        action="append",
        default=[],
        help="Extra openapi definition file to process (repeat for multiple)",
    )
    p.add_argument(
        "--output-basename-suffix",
        type=str,
        default=None,
        help="Customize suffix for generated file names",
    )
    p.add_argument(
        "--impl",
        type=str,
        default=None,
        help=(
            "Implementation variant for the given --src-type/--dst-type pair. "
            "For sql -> go/tests: "
            f"{', '.join(sql_tests_go.IMPLS)} "
            f"(default: {sql_tests_go.DEFAULT_IMPL}). "
            "For openapi -> openapi/standalone: "
            f"{', '.join(openapi_merge.IMPLS)} "
            f"(default: {openapi_merge.DEFAULT_IMPL})."
        ),
    )
    return p


def _expand_openapi_input(raw_input: str) -> tuple[list[Path], Path | str]:
    """Resolve the openapi input arg into a list of yaml files and the
    natural volume `src` root for those files.

    Returns `(files, root)` where `root` is:
      - the URL parent prefix for URL inputs,
      - the glob base directory for glob inputs,
      - the file path itself for single-file inputs (exact-match volume).

    `root` is used to inject an implicit volume mapping `root -> -o` so a
    user doesn't have to pass a redundant `-v` alongside `-o`.
    """
    if remote.is_url(raw_input):
        fetched = remote.fetch(raw_input)
        # Use the actual canonical URL that `fetch` resolved to (may differ
        # from `normalize_url` when the convenience-form fell back to master).
        canonical = remote.url_of_cache_path(fetched) or remote.normalize_url(raw_input)
        return [fetched], canonical

    if raw_input.endswith("/**"):
        base = Path(raw_input[: -len("/**")])
        if not base.is_dir():
            raise SystemExit(f"input glob base must be a directory: {base}")
        files = sorted(p for p in base.rglob("*.yaml") if p.is_file())
        if not files:
            raise SystemExit(f"input glob matched no .yaml files: {raw_input}")
        return files, base.absolute()

    if raw_input.endswith("/*"):
        base = Path(raw_input[: -len("/*")])
        if not base.is_dir():
            raise SystemExit(f"input glob base must be a directory: {base}")
        files = sorted(p for p in base.glob("*.yaml") if p.is_file())
        if not files:
            raise SystemExit(f"input glob matched no .yaml files: {raw_input}")
        return files, base.absolute()

    input_file = Path(raw_input)
    if not input_file.is_file():
        raise SystemExit(f"input must be a file: {input_file}")
    return [input_file], input_file.absolute()


def _run_openapi(args: argparse.Namespace) -> None:
    if args.dst_type == DST_OPENAPI_STANDALONE:
        _run_openapi_standalone(args)
        return
    if args.dst_type != DST_GO:
        raise SystemExit(
            f"--dst-type {args.dst_type} is not supported for openapi "
            f"(use {DST_GO} or {DST_OPENAPI_STANDALONE})"
        )

    input_files, input_root = _expand_openapi_input(args.input)

    volumes = _parse_volumes(args.volume)
    extra: list[Path | str] = [
        p if remote.is_url(p) else Path(p) for p in (args.extra or [])
    ]

    output: Path | None = args.output

    # `-o` resolves to either a single output file (definitions, suffixed path)
    # or an output directory. When it's a directory, we also inject a volume
    # mapping `input_root -> output` so files under the input scope get routed
    # to `-o` without the user having to repeat themselves with `-v`.
    output_is_file = (
        output is not None
        and (output.is_file() or (not output.exists() and output.suffix != ""))
    )

    if output is not None and not output_is_file:
        output.mkdir(parents=True, exist_ok=True)
        implicit = openapi_lib.Volume(src=input_root, dst=output.absolute())
        volumes = [implicit] + volumes

    if len(input_files) > 1 and output_is_file:
        raise SystemExit(
            f"input matched {len(input_files)} files; -o must be a directory "
            f"(or omitted) when input expands to multiple files"
        )

    for input_file in input_files:
        is_api = openapi_go._is_api_spec(input_file)

        if is_api:
            api_output_dir = output if (output is not None and not output_is_file) else None
            if api_output_dir is None:
                # No `-o` directory given; ask the volume system where this
                # api file should be written.
                if volumes:
                    openapi_lib.VOLUMES = list(volumes) + openapi_lib.VOLUMES
                try:
                    _, vol = openapi_lib.resolve_volume(input_file.absolute())
                except SystemExit:
                    raise SystemExit(
                        "openapi api spec needs -o/--output <directory> or a "
                        "matching --volume to determine its output directory"
                    )
                if vol.dst is None:
                    raise SystemExit(
                        f"api spec {input_file} matched a read-only/dst='-' volume; "
                        f"cannot infer an output directory"
                    )
                api_output_dir = vol.dst
                api_output_dir.mkdir(parents=True, exist_ok=True)
            if output_is_file:
                raise SystemExit(
                    f"-o {output} points at a file; api specs need an output directory"
                )
            openapi_go.run_api(
                input_file=input_file,
                output_dir=api_output_dir,
                volumes=volumes,
                extra=extra,
                output_basename_suffix=args.output_basename_suffix,
            )
            continue

        # Definitions input.
        if output_is_file:
            output.parent.mkdir(parents=True, exist_ok=True)
            openapi_go.run_definitions(
                input_file=input_file, output_file=output, volumes=volumes
            )
            continue
        # `-o` either was a dir (already injected as a volume above) or was
        # omitted; either way, let the volume system decide the output.
        openapi_go.run_definitions(input_file=input_file, output_file=None, volumes=volumes)


def _run_openapi_standalone(args: argparse.Namespace) -> None:
    if args.output is None:
        raise SystemExit(f"--dst-type {DST_OPENAPI_STANDALONE} requires -o/--output <file>")
    if args.output.exists() and args.output.is_dir():
        raise SystemExit(
            f"--dst-type {DST_OPENAPI_STANDALONE} requires -o to be a file path, got directory: {args.output}"
        )
    if args.volume:
        raise SystemExit(f"--volume is not supported for --dst-type {DST_OPENAPI_STANDALONE}")
    if args.extra:
        raise SystemExit(f"--extra is not supported for --dst-type {DST_OPENAPI_STANDALONE}")
    if args.output_basename_suffix is not None:
        raise SystemExit(
            f"--output-basename-suffix is not supported for --dst-type {DST_OPENAPI_STANDALONE}"
        )

    input_files, _ = _expand_openapi_input(args.input)
    if len(input_files) != 1:
        raise SystemExit(
            f"--dst-type {DST_OPENAPI_STANDALONE} requires exactly one input file "
            f"(got {len(input_files)})"
        )

    impl = args.impl or openapi_merge.DEFAULT_IMPL
    openapi_merge.run(input_file=input_files[0], output_file=args.output, impl=impl)


def _run_sql(args: argparse.Namespace) -> None:
    if args.dst_type != DST_GO_TESTS:
        raise SystemExit(f"--dst-type {args.dst_type} is not supported for sql (use {DST_GO_TESTS})")
    if args.output is None:
        raise SystemExit("sql codegen requires -o/--output <output_file>")
    if remote.is_url(args.input):
        raise SystemExit("sql codegen requires a local directory; URLs are not supported")
    sql_dir = Path(args.input)
    if not sql_dir.is_dir():
        raise SystemExit(f"input must be a directory for sql codegen: {sql_dir}")
    impl = args.impl or sql_tests_go.DEFAULT_IMPL
    sql_tests_go.run(sql_dir=sql_dir, output_file=args.output, impl=impl)


def _resolve_impl(args: argparse.Namespace) -> None:
    """Validate --impl against the (src_type, dst_type) pair."""
    pair = (args.src_type, args.dst_type)
    allowed = IMPLS_BY_PAIR.get(pair)
    if args.impl is None:
        return
    if allowed is None:
        raise SystemExit(
            f"--impl is not supported for --src-type={args.src_type} --dst-type={args.dst_type}"
        )
    if args.impl not in allowed:
        raise SystemExit(
            f"unsupported --impl {args.impl!r} for --src-type={args.src_type} "
            f"--dst-type={args.dst_type}; allowed: {', '.join(allowed)}"
        )


def _run_config(argv: list[str]) -> int:
    """`cg config <topic> [action] [args]` — manage persistent configuration."""
    parser = argparse.ArgumentParser(prog="cg config", description="manage cg configuration")
    sub = parser.add_subparsers(dest="topic", required=True)

    providers = sub.add_parser("providers", help="manage openapi source providers (tokens)")
    providers_actions = providers.add_subparsers(dest="action")
    add = providers_actions.add_parser(
        "add",
        help=(
            "add or update a provider. With no positional args, prompts "
            "interactively. With all three, runs non-interactively (suitable "
            "for CI)."
        ),
    )
    add.add_argument("provider", nargs="?", choices=config.PROVIDERS, help="provider kind")
    add.add_argument("domain", nargs="?", help="domain, e.g. gitlab.com or github.corp.example")
    add.add_argument("token", nargs="?", help="auth token")

    defaults = sub.add_parser("defaults", help="manage default behavioral config")
    defaults_actions = defaults.add_subparsers(dest="action")
    defaults_actions.add_parser(
        "init",
        help="create defaults config with system defaults if absent",
    )
    defaults_actions.add_parser(
        "reset",
        help="overwrite defaults config with system defaults",
    )
    patch = defaults_actions.add_parser(
        "patch",
        help="apply updates to defaults config",
    )
    patch.add_argument(
        "-t", "--type",
        dest="patch_type",
        choices=config.PATCH_TYPES,
        default="kv",
        help=(
            "how to interpret positional inputs (kv = <dotted.path>=<value>, "
            "patch-file = path to patch file [not yet implemented])"
        ),
    )
    patch.add_argument(
        "inputs",
        nargs="+",
        help=(
            "for -t kv: dotted-path assignments (cache.enabled=true, "
            "cache.ttl={d:14}); for -t patch-file: paths to patch files"
        ),
    )

    args = parser.parse_args(argv)
    if args.topic == "providers":
        if args.action is None:
            return config.cmd_list()
        if args.action == "add":
            return config.cmd_add(args.provider, args.domain, args.token)
        return 2
    if args.topic == "defaults":
        if args.action is None:
            return config.cmd_defaults_show()
        if args.action == "init":
            return config.cmd_defaults_init()
        if args.action == "reset":
            return config.cmd_defaults_reset()
        if args.action == "patch":
            return config.cmd_defaults_patch(args.patch_type, args.inputs)
        return 2
    return 2


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "config":
        sys.exit(_run_config(argv[1:]))
    args = _build_parser().parse_args(argv)
    _resolve_impl(args)
    openapi_lib.OUTPUT_TYPE = args.dst_type
    if args.src_type == SRC_OPENAPI:
        _run_openapi(args)
    elif args.src_type == SRC_SQL:
        _run_sql(args)
    else:
        raise SystemExit(f"unsupported --src-type: {args.src_type}")


if __name__ == "__main__":
    main()
