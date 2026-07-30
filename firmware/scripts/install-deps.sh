#!/usr/bin/env bash
# install-deps.sh — install the host tooling used to deploy node firmware.
#
#   circup     installs the Adafruit CircuitPython libraries onto a mounted board
#   mpy-cross  (optional) compiles the node modules to .mpy for --mpy builds
#
# Run this once on the machine you flash Picos from. Then use build.sh to deploy.
set -euo pipefail

echo "==> Checking Python"
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
python3 -m pip --version >/dev/null 2>&1 || { echo "pip not found; install python3-pip"; exit 1; }

echo "==> Installing circup (Adafruit library manager)"
python3 -m pip install --user --upgrade circup

echo "==> Installing mpy-cross (optional; only needed for --mpy builds)"
if ! python3 -m pip install --user --upgrade mpy-cross; then
  echo "   (mpy-cross install failed — that's fine; plain .py builds still work)"
fi

echo
echo "Done."
echo "Make sure ~/.local/bin is on your PATH so 'circup' is found:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
echo
echo "Next: mount a Pico's CIRCUITPY drive and run:"
echo "  ./build.sh --node <node-id>"
