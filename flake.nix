{
  description = "cg — minimal codegen tool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        callPackage = args: import ./package.nix ({ inherit (pkgs) lib python3; } // args);

        cg = callPackage { };
        cgLib = callPackage { kind = "library"; };
      in {
        packages = {
          default = cg;
          cg = cg;
          cg-lib = cgLib;
        };

        apps.default = {
          type = "app";
          program = "${cg}/bin/cg";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pyyaml ps.jinja2 ]))
          ];
        };
      });
}
