#!/bin/bash
# Download a portable CPython ("python-build-standalone") for the standalone
# (bundled) desktop build.  Only needed once per platform/target.
#
# Override PYTHON_VERSION, TAG or TARGET from the environment.  See
# <https://github.com/astral-sh/python-build-standalone/releases>.

set -euo pipefail

cd "$(dirname "$0")/.."  # desktop/

PYTHON_VERSION="${PYTHON_VERSION:-3.13.7}"
TAG="${TAG:-20250828}"
TARGET="${TARGET:-x86_64-unknown-linux-gnu}"

DEST_DIR="src-tauri/pyembed"
url="https://github.com/astral-sh/python-build-standalone/releases/download/${TAG}/cpython-${PYTHON_VERSION}+${TAG}-${TARGET}-install_only_stripped.tar.gz"

mkdir -p "$DEST_DIR"
curl -L "$url" | tar -xz -C "$DEST_DIR"
echo "python extracted to $DEST_DIR/python"
