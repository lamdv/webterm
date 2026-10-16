# desktop packaging

The Tauri desktop app is developed against the prebuilt `pytauri-wheel` (no
Rust toolchain needed) and packaged as a standalone bundle with an embedded
CPython.

- `src-tauri/` — the Rust project (PyTauri stub) used for the standalone build.
- `scripts/download-py.sh` — fetch a portable CPython for the current target.
- `scripts/build.sh` — build the standalone app locally (needs Rust + tauri-cli).
- `.github/workflows/desktop-release.yml` — the supported packaging path: builds
  per-OS bundles in CI on a tag or manual dispatch.

During development, run `python -m webterm_desktop` from `desktop/` (see the
top-level proposal). The standalone path is CI-only for now and has not been
built locally in this checkout.
