{
  description = "ratchet — mine Claude Code sessions into reviewable config improvements";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];
    in {
      devShells = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          # V0 is stdlib-only; the shell just pins an interpreter.
          default = pkgs.mkShell { packages = [ pkgs.python3 ]; };
        });

      # `nix run .#tap [-- ARGS]` — the transcript fetcher as a real command.
      apps = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          tap = {
            type = "app";
            program = "${pkgs.writeShellScript "tap" ''
              export PYTHONPATH=${self}
              exec ${pkgs.python3}/bin/python -m ratchet.tap "$@"
            ''}";
          };
        });
    };
}
