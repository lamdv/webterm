#!/bin/bash
# Build the standalone (bundled) desktop app locally.
#
# Prerequisites: Rust + tauri-cli, uv, the platform webview dev libraries, and
# a portable Python downloaded with `scripts/download-py.sh`.
#
# This is the same sequence the desktop-release CI workflow runs; CI is the
# supported path and the one that is actually exercised.

set -euo pipefail

cd "$(dirname "$0")/.."  # desktop/

PRODUCT_NAME="webterm"
PYLIB_DIR="$(realpath src-tauri/pyembed/python/lib)"

export PYTAURI_STANDALONE=1
export PYO3_PYTHON="$(realpath src-tauri/pyembed/python/bin/python3)"
export RUSTFLAGS=" \
    -C link-arg=-Wl,-rpath,\$ORIGIN/../lib/$PRODUCT_NAME/lib \
    -L $PYLIB_DIR"

# Install the desktop package and the webterm server into the bundled Python.
uv pip install \
    --exact \
    --compile-bytecode \
    --python="$PYO3_PYTHON" \
    ./src-tauri \
    ..

# Generate the full icon set from the single source PNG.
npx --yes @tauri-apps/cli@^2 icon src/webterm_desktop/icons/icon.png -o src-tauri/icons

cargo tauri build --config="src-tauri/tauri.bundle.json" -- --profile bundle-release
