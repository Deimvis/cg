"""Test harness for end-to-end CLI tests.

Each test gets:
  - a private XDG_CONFIG_HOME pointed at a tmp_path subdir (via monkeypatch),
  - a `run` callable that invokes `cg.cli.main(argv)` in-process and returns
    `(exit_code, stdout, stderr)`.

In-process invocation lets us assert on `cg.config` module state too — e.g.,
that `set_defaults_path_override` was restored after `cg with` returned.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Callable

import pytest

from cg import cli, config


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XDG_CONFIG_HOME at a per-test directory. Returns the dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # `defaults_path()` consults XDG_CONFIG_HOME each call, so no further
    # caching to clear. But make sure the override is clean (in case a
    # prior test left it set after a crash).
    config.set_defaults_path_override(None)
    return tmp_path


@pytest.fixture
def defaults_file(xdg: Path) -> Path:
    return xdg / "cg" / "defaults.json"


Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run() -> Run:
    """Invoke `cg.cli.main(list(argv))` in-process. Catches SystemExit so the
    test can assert on the exit code. Returns (code, stdout, stderr)."""

    def _run(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(list(argv))
            except SystemExit as e:
                v = e.code
                if v is None:
                    code = 0
                elif isinstance(v, int):
                    code = v
                else:
                    # argparse passes strings; treat any non-int as failure
                    # and append the message to stderr like argparse does.
                    err.write(str(v) + "\n")
                    code = 1
        return code, out.getvalue(), err.getvalue()

    return _run


def read_json(p: Path) -> dict:
    with p.open() as f:
        return json.load(f)
