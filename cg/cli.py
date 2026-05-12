import argparse
from pathlib import Path

from . import openapi_go, openapi_lib, remote, sql_tests_go


SRC_OPENAPI = "openapi"
SRC_SQL = "sql"

DST_GO = "go"
DST_GO_TESTS = "go/tests"


def _parse_volumes(raw: list[str] | None) -> list[openapi_lib.Volume]:
    if not raw:
        return []
    return [openapi_lib.Volume(**remote.parse_volume(v)) for v in raw]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cg", description="codegen entrypoint")
    p.add_argument("--src-type", required=True, choices=[SRC_OPENAPI, SRC_SQL])
    p.add_argument("--dst-type", required=True, choices=[DST_GO, DST_GO_TESTS])
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
    return p


def _expand_openapi_input(raw_input: str) -> list[Path]:
    """Resolve the openapi input arg into a list of local yaml files.

    Accepts: an http(s) URL (fetched), a single file path, or a glob
    suffix '<prefix>/*' (one directory) / '<prefix>/**' (recursive).
    """
    if remote.is_url(raw_input):
        return [remote.fetch(raw_input)]

    if raw_input.endswith("/**"):
        base = Path(raw_input[: -len("/**")])
        if not base.is_dir():
            raise SystemExit(f"input glob base must be a directory: {base}")
        files = sorted(p for p in base.rglob("*.yaml") if p.is_file())
        if not files:
            raise SystemExit(f"input glob matched no .yaml files: {raw_input}")
        return files

    if raw_input.endswith("/*"):
        base = Path(raw_input[: -len("/*")])
        if not base.is_dir():
            raise SystemExit(f"input glob base must be a directory: {base}")
        files = sorted(p for p in base.glob("*.yaml") if p.is_file())
        if not files:
            raise SystemExit(f"input glob matched no .yaml files: {raw_input}")
        return files

    input_file = Path(raw_input)
    if not input_file.is_file():
        raise SystemExit(f"input must be a file: {input_file}")
    return [input_file]


def _run_openapi(args: argparse.Namespace) -> None:
    if args.dst_type != DST_GO:
        raise SystemExit(f"--dst-type {args.dst_type} is not supported for openapi (use {DST_GO})")

    input_files = _expand_openapi_input(args.input)

    volumes = _parse_volumes(args.volume)
    extra: list[Path | str] = [
        p if remote.is_url(p) else Path(p) for p in (args.extra or [])
    ]

    output: Path | None = args.output

    if len(input_files) > 1 and output is not None and not output.is_dir():
        raise SystemExit(
            f"input glob matched {len(input_files)} files; "
            f"-o must be a directory (or omitted) when input expands to multiple files"
        )

    for input_file in input_files:
        if output is not None and output.is_dir():
            openapi_go.run_api(
                input_file=input_file,
                output_dir=output,
                volumes=volumes,
                extra=extra,
                output_basename_suffix=args.output_basename_suffix,
            )
            continue

        if output is not None:
            openapi_go.run_definitions(input_file=input_file, output_file=output)
            continue

        openapi_go.run_auto(
            input_file=input_file,
            output=None,
            volumes=volumes,
            extra=extra,
            output_basename_suffix=args.output_basename_suffix,
        )


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
    sql_tests_go.run(sql_dir=sql_dir, output_file=args.output)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.src_type == SRC_OPENAPI:
        _run_openapi(args)
    elif args.src_type == SRC_SQL:
        _run_sql(args)
    else:
        raise SystemExit(f"unsupported --src-type: {args.src_type}")


if __name__ == "__main__":
    main()
