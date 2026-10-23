/* Smoke test for the adapter view registry (webterm/static/views.js). */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const scripts = [];
global.document = {
  createElement(tag) {
    const node = {
      tagName: String(tag).toUpperCase(),
      listeners: Object.create(null),
      src: "",
      addEventListener(type, handler) {
        this.listeners[type] = handler;
      },
    };
    if (node.tagName === "SCRIPT") scripts.push(node);
    return node;
  },
  head: {
    appendChild() {},
  },
  body: {
    appendChild() {},
  },
};
global.window = global;

require(path.join(__dirname, "..", "webterm", "static", "views.js"));

const views = window.WebtermAgentViews;
assert.ok(views, "the registry is exposed");

// ------------------------------------------------------------- registration
const canonical = { transport: "canonical", create: () => "generic" };
const pi = { transport: "pi", label: "pi", create: () => "pi" };
views.register("", canonical);
views.register("pi", pi);
views.register("mva", { create: () => "mva" });

assert.strictEqual(views.base().create(), "generic", "the base view is registered under ''");
assert.strictEqual(views.get("pi").transport, "pi", "transport is kept");
assert.strictEqual(views.get("pi").label, "pi", "labels are kept");
assert.strictEqual(views.get("mva").transport, "canonical", "transport defaults to canonical");
assert.strictEqual(views.get("mva").label, "", "labels default to empty");
assert.deepStrictEqual(
  views.adapters().sort(),
  ["mva", "pi"],
  "adapters() lists the registered backends, not the base"
);

// ------------------------------------------------------------- resolution
assert.strictEqual(views.resolve("pi").create(), "pi", "a registered adapter resolves to itself");
assert.strictEqual(
  views.resolve("unknown").create(),
  "generic",
  "an unknown adapter falls back to the base view"
);
assert.strictEqual(views.get("unknown"), null, "get() does not fall back");

assert.throws(
  () => views.register("broken", {}),
  /create\(\)/,
  "a view without create() is rejected"
);

// ---------------------------------------------------------------- preload
(async () => {
  const first = views.preload([
    { name: "remote", available: true, view: "/remote.js" },
    { name: "off", available: false, view: "/off.js" },
    { name: "nofile", available: true, view: null },
  ]);

  // Only the available adapter with a module path is injected.
  assert.strictEqual(scripts.length, 1, "one module is injected for the unloaded adapter");
  assert.strictEqual(scripts[0].src, "/remote.js", "the advertised module path is used");

  // Loading it registers the view; a second preload is a no-op.
  views.register("remote", { transport: "canonical", create: () => "remote" });
  scripts[0].listeners.load();
  await first;
  await views.preload([{ name: "remote", available: true, view: "/remote.js" }]);
  assert.strictEqual(scripts.length, 1, "an already-loaded module is not injected twice");
  assert.strictEqual(views.resolve("remote").create(), "remote", "the module's view is used");

  // A failing module resolves quietly so the base view keeps working.
  const pending = views.preload([{ name: "broken", available: true, view: "/broken.js" }]);
  assert.strictEqual(scripts.length, 2, "a fresh module URL is injected");
  scripts[1].listeners.error();
  await pending;
  assert.strictEqual(views.resolve("broken").create(), "generic", "a failed load falls back");

  console.log("views smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
