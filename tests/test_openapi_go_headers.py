"""Tests for Go codegen of request header parameters.

Covers the structured-header path (`fw.RequestStructHeader`) and the
no-header fallback (`fw.RequestHeader[fw.JSONHeaderPreset]`) produced by
`openapi_lib.handle_api_file`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cg import openapi_lib


def _write_spec(path: Path, body: str) -> None:
    path.write_text(body)


def _generate(tmp_path: Path, spec: str) -> str:
    api = tmp_path / "api.yaml"
    _write_spec(api, spec)
    out = tmp_path / "out"
    out.mkdir()
    openapi_lib.handle_api_file(api, out, openapi_lib.ProgrammingLanguage.Go)
    files = list(out.glob("*.go"))
    assert len(files) == 1, f"expected one generated file, got {files}"
    return files[0].read_text()


def test_structured_header_uses_preset_as_first_type_argument(tmp_path: Path) -> None:
    spec = """
openapi: 3.0.0
info: {title: t, version: '1'}
paths:
  /items:
    get:
      parameters:
        - in: header
          name: X-EndClient-AppInterface
          schema: {type: string}
        - in: header
          name: X-EndClient-AppSlug
          schema: {type: string}
      responses:
        '200':
          content:
            application/json:
              schema: {type: object, properties: {ok: {type: boolean}}}
"""
    code = _generate(tmp_path, spec)
    assert "fw.RequestStructHeader[fw.JSONHeaderPreset, struct {" in code, code
    assert "fwheader.WithExtras" in code
    assert 'XEndClientAppInterface *string `header:"X-EndClient-AppInterface"`' in code
    assert 'XEndClientAppSlug *string `header:"X-EndClient-AppSlug"`' in code
    assert '/fwheader"' in code
    # Old single-arg form must not slip back in.
    assert "fw.RequestStructHeader[struct" not in code
    # Old named EXTRA field must not slip back in.
    assert "EXTRA http.Header" not in code


def test_required_header_is_not_pointer(tmp_path: Path) -> None:
    spec = """
openapi: 3.0.0
info: {title: t, version: '1'}
paths:
  /items:
    get:
      parameters:
        - in: header
          name: X-Request-Id
          required: true
          schema: {type: string}
      responses:
        '200':
          content:
            application/json:
              schema: {type: object, properties: {ok: {type: boolean}}}
"""
    code = _generate(tmp_path, spec)
    assert 'XRequestId string `header:"X-Request-Id"`' in code, code
    assert "fw.RequestStructHeader[fw.JSONHeaderPreset, struct {" in code


def test_no_headers_falls_back_to_preset_form(tmp_path: Path) -> None:
    spec = """
openapi: 3.0.0
info: {title: t, version: '1'}
paths:
  /ping:
    get:
      responses:
        '200':
          content:
            application/json:
              schema: {type: object, properties: {pong: {type: boolean}}}
"""
    code = _generate(tmp_path, spec)
    assert "fw.RequestHeader[fw.JSONHeaderPreset]" in code, code
    assert "fw.RequestStructHeader" not in code
    assert "fwheader" not in code


def test_header_slice_type_is_rejected(tmp_path: Path) -> None:
    spec = """
openapi: 3.0.0
info: {title: t, version: '1'}
paths:
  /items:
    get:
      parameters:
        - in: header
          name: X-Tags
          schema:
            type: array
            items: {type: string}
      responses:
        '200':
          content:
            application/json:
              schema: {type: object, properties: {ok: {type: boolean}}}
"""
    api = tmp_path / "api.yaml"
    _write_spec(api, spec)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError, match="unsupported structured header type"):
        openapi_lib.handle_api_file(api, out, openapi_lib.ProgrammingLanguage.Go)
