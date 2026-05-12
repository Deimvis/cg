import subprocess as sp
from pathlib import Path

import yaml

from . import openapi_lib, remote
from .openapi_lib import (
    GENERATED_OUT_FILES,
    ProgrammingLanguage,
    get_out_fp,
    handle_api_file,
    handle_definitions_file,
)


def _is_api_spec(input_file: Path) -> bool:
    content = openapi_lib.load_yaml(input_file) or {}
    return isinstance(content, dict) and "paths" in content


def _gofmt(*paths: Path) -> None:
    for p in paths:
        sp.run(["gofmt", "-s", "-w", p.absolute()])
        sp.run(["goimports", "-w", p.absolute()])


def run_api(
    input_file: Path,
    output_dir: Path,
    volumes: list[openapi_lib.Volume] | None = None,
    extra: list[Path | str] | None = None,
    output_basename_suffix: str | None = None,
) -> None:
    assert input_file.is_file()
    assert output_dir.is_dir()

    if volumes:
        openapi_lib.VOLUMES = list(volumes) + openapi_lib.VOLUMES
    if output_basename_suffix is not None:
        openapi_lib.CODEGEN_FILE_NAME_SUFFIX = output_basename_suffix

    handle_api_file(input_file.absolute(), output_dir.absolute(), ProgrammingLanguage.Go)

    if extra:
        for p in extra:
            if isinstance(p, str) and remote.is_url(p):
                fp = remote.fetch(p)
            else:
                fp = Path(p) if not isinstance(p, Path) else p
                assert fp.is_file()
                fp = fp.absolute()
            handle_definitions_file(fp, get_out_fp(fp), ProgrammingLanguage.Go)

    _gofmt(output_dir.absolute())
    for d in {f.parent for f in GENERATED_OUT_FILES}:
        _gofmt(d.absolute())


def run_definitions(
    input_file: Path,
    output_file: Path | None = None,
    volumes: list[openapi_lib.Volume] | None = None,
) -> None:
    assert input_file.is_file()
    if volumes:
        openapi_lib.VOLUMES = list(volumes) + openapi_lib.VOLUMES
    if output_file is None:
        openapi_lib.DEFAULT_VOLUME = []
        output_file = get_out_fp(input_file)
    else:
        output_file.parent.mkdir(parents=True, exist_ok=True)

    handle_definitions_file(input_file, output_file, ProgrammingLanguage.Go)

    _gofmt(output_file.parent.absolute())
    for d in {f.parent for f in GENERATED_OUT_FILES}:
        _gofmt(d.absolute())


def run_auto(
    input_file: Path,
    output: Path | None,
    volumes: list[openapi_lib.Volume] | None = None,
    extra: list[Path | str] | None = None,
    output_basename_suffix: str | None = None,
) -> None:
    if _is_api_spec(input_file):
        if output is None or not output.is_dir():
            raise SystemExit("openapi api spec requires -o/--output <existing_directory>")
        run_api(
            input_file=input_file,
            output_dir=output,
            volumes=volumes,
            extra=extra,
            output_basename_suffix=output_basename_suffix,
        )
    else:
        out_file = output if (output is not None and not output.is_dir()) else None
        run_definitions(input_file=input_file, output_file=out_file, volumes=volumes)
