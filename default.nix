{ pkgs ? import <nixpkgs> { } }:

import ./package.nix { inherit (pkgs) lib python3; }
