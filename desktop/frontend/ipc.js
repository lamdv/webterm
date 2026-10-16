/*
 * The IPC bridge.
 *
 * Tauri injects `window.__TAURI_INTERNALS__` into every window it creates,
 * including the picker.  The official `@tauri-apps/api` package's `invoke` is a
 * thin wrapper around `__TAURI_INTERNALS__.invoke`, and that is exactly what we
 * need.  Calling it directly keeps this frontend buildless: no npm install, no
 * bundler, no vendored megabyte of API surface.
 *
 * The bridge is present even after the window navigates to a server, but the
 * capability in `capabilities/default.json` has no `remote` scope, so Tauri
 * rejects every command from a remote origin.
 */
export function invoke(command, args = {}) {
  const internals = window.__TAURI_INTERNALS__;
  if (!internals) {
    return Promise.reject(
      new Error("the Tauri IPC bridge is unavailable in this window"),
    );
  }
  return internals.invoke(command, args);
}
