{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  packages = [
    (pkgs.python3.withPackages (ps: [ ps.pyyaml ps.jinja2 ps.pytest ]))
    pkgs.gnumake
  ];
}
