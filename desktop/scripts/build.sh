#!/bin/bash
# Build the desktop app locally.
#
# Prerequisites: rustup, the Tauri CLI, and the platform webview development
# libraries (on Arch: `webkit2gtk-4.1`).  See tutorial/00-orientation.md.

set -euo pipefail

cd "$(dirname "$0")/.." # desktop/

# `cargo tauri` assumes the CLI was installed with `cargo install tauri-cli`.
# If you installed it with npm instead, use `npx tauri build`.
cargo tauri build "$@"
