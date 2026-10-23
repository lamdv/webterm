/* Adapter view registry for structured agent sessions.
 *
 * app.js never names a backend.  A view module registers itself here:
 *
 *   window.WebtermAgentViews.register("pi", { transport: "pi", create });
 *
 * `transport` says which envelope the view speaks — "canonical" (the shared
 * `agent` command/event stream) or "pi" (the legacy raw pi envelope) — so
 * app.js can build the right hooks without knowing the adapter.
 *
 * The server advertises adapters and an optional `view` module in
 * `/config.js` (`agentAdapters`).  `preload()` injects those modules on
 * demand, which is what lets a new backend ship one file and one config
 * entry instead of editing app.js and index.html.  An adapter with no
 * registered view, or whose module fails to load, falls back to the base
 * view registered under "".
 */
"use strict";

(function () {
  const TRANSPORTS = ["canonical", "pi"];
  const views = new Map();
  const loading = new Map();

  function normalizeAdapter(name) {
    return String(name === undefined || name === null ? "" : name);
  }

  function register(adapter, view) {
    const entry = view && typeof view === "object" ? view : {};
    const create = entry.create;
    if (typeof create !== "function") {
      throw new Error("a view needs a create() function");
    }
    const transport = TRANSPORTS.includes(entry.transport)
      ? entry.transport
      : "canonical";
    views.set(normalizeAdapter(adapter), {
      transport,
      create,
      label: typeof entry.label === "string" ? entry.label : "",
    });
  }

  function get(adapter) {
    return views.get(normalizeAdapter(adapter)) || null;
  }

  /** The view every adapter falls back to; registered by the shared view. */
  function base() {
    return views.get("") || null;
  }

  /** Resolve an adapter to a view, falling back to the base view. */
  function resolve(adapter) {
    return get(adapter) || base();
  }

  function adapters() {
    return Array.from(views.keys()).filter(Boolean);
  }

  /** Inject one classic script once, resolving even when it fails to load. */
  function loadScript(url) {
    const key = String(url);
    if (!key) return Promise.resolve(false);
    const pending = loading.get(key);
    if (pending) return pending;
    const promise = new Promise((resolveLoad) => {
      if (typeof document === "undefined") {
        resolveLoad(false);
        return;
      }
      const script = document.createElement("script");
      script.src = key;
      script.async = true;
      script.addEventListener("load", () => resolveLoad(true));
      script.addEventListener("error", () => resolveLoad(false));
      (document.head || document.body).appendChild(script);
    });
    loading.set(key, promise);
    return promise;
  }

  /**
   * Load the view modules the server advertises, so a session that appears
   * later resolves to its own view.  Missing or broken modules are ignored:
   * the base view still handles the session, and the declared commands in
   * the resync keep its features reachable.
   */
  function preload(entries) {
    const list = Array.isArray(entries) ? entries : [];
    const wanted = [];
    for (const entry of list) {
      if (!entry || typeof entry !== "object") continue;
      if (entry.available === false) continue;
      if (typeof entry.view !== "string" || !entry.view) continue;
      if (get(entry.name)) continue;
      wanted.push(loadScript(entry.view));
    }
    return Promise.all(wanted).then((results) => results.some(Boolean));
  }

  window.WebtermAgentViews = {
    register,
    get,
    base,
    resolve,
    adapters,
    preload,
    loadScript,
  };
})();
