import argparse
from pathlib import Path

from . import openapi_go, sql_tests_go


SRC_OPENAPI = "openapi"
SRC_SQL = "sql"

DST_GO = "go"
DST_GO_TESTS = "go/tests"


def _parse_volumes(raw: list[str] | None) -> list[tuple[Path, Path]]:
    if not raw:
        return []
    out: list[tuple[Path, Path]] = []
    for v in raw:
        src, dst = v.split(":")
        out.append((Path.cwd() / src, Path.cwd() / dst))
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cg", description="codegen entrypoint")
    p.add_argument("--src-type", required=True, choices=[SRC_OPENAPI, SRC_SQL])
    p.add_argument("--dst-type", required=True, choices=[DST_GO, DST_GO_TESTS])
    p.add_argument("input", type=Path, help="Input file or directory")
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
        nargs="*",
        action="extend",
        help="Volumes in form `src:dst` for openapi codegen",
    )
    p.add_argument(
        "-e",
        "--extra",
        type=str,
        nargs="*",
        action="extend",
        help="Extra openapi definition files to process",
    )
    p.add_argument(
        "--output-basename-suffix",
        type=str,
        default=None,
        help="Customize suffix for generated file names",
    )
    return p


def _run_openapi(args: argparse.Namespace) -> None:
    if args.dst_type != DST_GO:
        raise SystemExit(f"--dst-type {args.dst_type} is not supported for openapi (use {DST_GO})")

    input_file: Path = args.input
    if not input_file.is_file():
        raise SystemExit(f"input must be a file: {input_file}")

    volumes = _parse_volumes(args.volume)
    extra = [Path(p) for p in (args.extra or [])]

    output: Path | None = args.output
    if output is not None and output.is_dir():
        openapi_go.run_api(
            input_file=input_file,
            output_dir=output,
            volumes=volumes,
            extra=extra,
            output_basename_suffix=args.output_basename_suffix,
        )
        return

    if output is not None:
        openapi_go.run_definitions(input_file=input_file, output_file=output)
        return

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
    sql_dir: Path = args.input
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
