{
  lib,
  python3Packages,
  bluez,
}:

python3Packages.buildPythonApplication {
  pname = "sonyd";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      ./sonyd
    ];
  };

  build-system = [ python3Packages.setuptools ];

  # sdptool (bluez) sert à trouver le canal RFCOMM du casque
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [ bluez ])
  ];

  pythonImportsCheck = [ "sonyd" ];

  meta = {
    description = "Control Sony WH-1000XM6 headphones from Linux";
    mainProgram = "sonyd";
    platforms = lib.platforms.linux;
  };
}
