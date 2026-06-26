{
  description = "ratchet — mine Claude Code sessions into reviewed config improvements";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];
    in {
      devShells = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = pkgs.mkShell {
            # V0 is stdlib-only; the shell just pins an interpreter.
            packages = [ pkgs.python3 ];
          };
        });
    };
}
