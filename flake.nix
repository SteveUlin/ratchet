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

      # `nix run .#<block> [-- ARGS]` — each composable block as a real command.
      apps = forAll (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          block = name: {
            type = "app";
            program = "${pkgs.writeShellScript name ''
              export PYTHONPATH=${self}
              exec ${pkgs.python3}/bin/python -m ratchet.${name} "$@"
            ''}";
          };
        in {
          tap = block "tap";      # fetch:     datastore → raw blob
          weave = block "weave";  # render:    raw blob → cleaned blob
          chunk = block "chunk";  # window:    cleaned blob → chunkset
          glean = block "glean";  # extract:   chunkset → events (LLM)
          dream = block "dream";  # synthesize: events → takeaways (LLM)
          review = block "review"; # gate:      takeaways → concepts (human + the /ratchet-review skill)
          garden = block "garden"; # tend:      concepts → managed tags + structural-op proposals (LLM)
          # generate is a GLOBAL projection, not a Block (ADR-0020) — but the same command wrapper runs it.
          generate = block "generate"; # project:   valid concepts → a marked CLAUDE.md region (no LLM)
        });
    };
}
