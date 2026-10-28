{
  description = "Control Sony WH-1000XM6 headphones from Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (
          system: f nixpkgs.legacyPackages.${system}
        );
    in
    {
      packages = forAllSystems (pkgs: rec {
        sonyd = pkgs.callPackage ./package.nix { };
        default = sonyd;
      });

      overlays.default = final: prev: {
        sonyd = final.callPackage ./package.nix { };
      };
    };
}
