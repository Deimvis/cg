{ lib, python3, kind ? "application" }:

let
  pyproject = lib.importTOML ./pyproject.toml;
  project = pyproject.project;

  builder =
    if kind == "application" then python3.pkgs.buildPythonApplication
    else if kind == "library" then python3.pkgs.buildPythonPackage
    else throw "package.nix: kind must be \"application\" or \"library\", got ${kind}";
in
builder {
  pname = project.name;
  version = project.version;
  pyproject = true;
  src = ./.;

  build-system = [ python3.pkgs.setuptools ];
  dependencies = [ python3.pkgs.pyyaml python3.pkgs.jinja2 ];

  doCheck = false;

  meta = {
    inherit (project) description;
    mainProgram = "cg";
  };
}
