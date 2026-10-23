/* Minimal DOM shim + smoke test for the canonical view's adapter-owned UI
 * state: keyed status chips, composer widgets, the view title, and the
 * composer prefill.  All of it is shape-only, so no adapter name appears. */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this._classes = new Set();
    this.textContent = "";
    this._listeners = Object.create(null);
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.placeholder = "";
    this.rows = 0;
    this.type = "";
    this.title = "";
    this.focused = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.open = false;
    this.selected = false;
    if (this.tagName === "SELECT") {
      // A browser keeps only a value one of its options carries and marks that
      // option `selected`; model both so the tests can read the real choice.
      let value = "";
      Object.defineProperty(this, "value", {
        configurable: true,
        enumerable: true,
        get: () => value,
        set: (next) => {
          const wanted = next === undefined || next === null ? "" : String(next);
          const known = this.children.some(
            (child) => child && child.tagName === "OPTION" && String(child.value) === wanted
          );
          value = known ? wanted : "";
          for (const child of this.children) {
            if (child && child.tagName === "OPTION") child.selected = String(child.value) === value;
          }
        },
      });
    }
  }

  get className() {
    return [...this._classes].join(" ");
  }

  set className(value) {
    this._classes = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  get classList() {
    const classes = this._classes;
    return {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, force) => {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name);
        else classes.delete(name);
        return enabled;
      },
      contains: (name) => classes.has(name),
    };
  }

  set innerHTML(value) {
    if (value === "") this.children = [];
  }

  get innerHTML() {
    return "";
  }

  append(...nodes) {
    for (const node of nodes) {
      this.children.push(node);
      if (node && typeof node === "object") node.parent = this;
    }
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  prepend(node) {
    this.children.unshift(node);
    if (node && typeof node === "object") node.parent = this;
  }

  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
      this.parent = null;
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  setAttribute() {}

  focus() {
    this.focused = true;
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  dispatch(type, event) {
    const handler = this._listeners[type];
    if (handler) handler(event || { preventDefault() {} });
  }

  querySelector(selector) {
    const found = this.querySelectorAll(selector);
    return found.length ? found[0] : null;
  }

  querySelectorAll(selector) {
    const value = String(selector || "");
    let match = null;
    if (value.startsWith(".")) {
      const names = value
        .slice(1)
        .split(".")
        .filter(Boolean);
      match = (node) => node._classes && names.every((name) => node._classes.has(name));
    } else {
      const tagName = value.toUpperCase();
      match = (node) => node.tagName === tagName;
    }
    const found = [];
    const walk = (node) => {
      if (node && node._classes && match(node)) found.push(node);
      for (const child of (node && node.children) || []) {
        if (child && typeof child === "object") walk(child);
      }
    };
    walk(this);
    return found;
  }
}

global.document = {
  createElement: (tag) => new FakeNode(tag),
};
global.window = global;
global.WebtermHighlight = null;
global.requestAnimationFrame = (callback) => callback();

require(path.join(__dirname, "..", "webterm", "static", "agent.js"));

function walk(node, visit) {
  visit(node);
  for (const child of (node && node.children) || []) {
    if (child && typeof child === "object") walk(child, visit);
  }
}

function findByClass(root, className) {
  let found = null;
  walk(root, (node) => {
    if (!found && node && node._classes && node._classes.has(className)) found = node;
  });
  return found;
}

function textOf(root) {
  let text = "";
  walk(root, (node) => {
    if (node && typeof node.textContent === "string") text += node.textContent;
    if (node && node.value) text += node.value;
    text += "\n";
  });
  return text;
}

/** Let resolved command promises and their follow-ups settle. */
function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

function keydown(node, key, extra) {
  node.dispatch(
    "keydown",
    Object.assign(
      {
        key,
        shiftKey: false,
        ctrlKey: false,
        metaKey: false,
        preventDefault() {},
      },
      extra || {}
    )
  );
}

function type(node, value) {
  node.value = value;
  node.dispatch("input");
}

function paletteNames(view) {
  const palette = findByClass(view.element, "agent-palette");
  if (!palette || palette.hidden) return null;
  return palette
    .querySelectorAll(".agent-palette-item")
    .map((item) => item.children[0].textContent);
}


function byClassAll(root, className) {
  return root.querySelectorAll ? root.querySelectorAll("." + className) : [];
}

(async () => {
  const view = window.WebtermAgent.create(
    { id: "ui-1", title: "fallback title" },
    {
      onPrompt: () => Promise.resolve(),
      onAbort: () => Promise.resolve(),
      onCommand: () => Promise.resolve({}),
    }
  );

  view.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, ui: { status: true, widgets: true, title: true, editor: true } },
    ui: {
      status: { build: "building…", tests: "12 passing" },
      widgets: {
        todo: { text: "one\ntwo", placement: "above" },
        note: { text: "below the composer", placement: "below" },
      },
      title: "resynced title",
    },
    dialogs: [],
    cursor: { seq: 0 },
  });

  const title = findByClass(view.element, "pi-title");
  assert.strictEqual(title.textContent, "resynced title", "the resync title wins");

  const statusHost = findByClass(view.element, "agent-ui-statuses");
  assert.strictEqual(statusHost.hidden, false, "status chips are shown");
  assert.deepStrictEqual(
    byClassAll(view.element, "agent-ui-chip").map((chip) => chip.textContent),
    ["building…", "12 passing"],
    "chips render in adapter order"
  );
  assert.deepStrictEqual(
    byClassAll(view.element, "agent-ui-chip").map((chip) => chip.dataset.key),
    ["build", "tests"],
    "each chip keeps its key"
  );

  const widgetHosts = byClassAll(view.element, "agent-ui-widgets");
  const above = widgetHosts.find((node) => node.classList.contains("above"));
  const below = widgetHosts.find((node) => node.classList.contains("below"));
  assert.strictEqual(above.hidden, false, "above widgets are shown");
  assert.strictEqual(below.hidden, false, "below widgets are shown");
  assert.deepStrictEqual(
    byClassAll(above, "agent-ui-widget").map((node) => node.textContent),
    ["one\ntwo"],
    "the above widget keeps its lines"
  );
  assert.deepStrictEqual(
    byClassAll(below, "agent-ui-widget").map((node) => node.textContent),
    ["below the composer"],
    "the below widget lands in the lower host"
  );

  // Live updates: new keys appear, an empty text clears the key.
  view.handle({ type: "agent_event", event: { event: "status.updated", key: "build", text: "done" } });
  view.handle({
    type: "agent_event",
    event: { event: "status.updated", key: "extension", text: "active" },
  });
  assert.deepStrictEqual(
    byClassAll(view.element, "agent-ui-chip").map((chip) => chip.textContent),
    ["done", "12 passing", "active"],
    "status.updated replaces one chip and appends another"
  );
  view.handle({
    type: "agent_event",
    event: { event: "status.updated", key: "tests", text: "" },
  });
  assert.deepStrictEqual(
    byClassAll(view.element, "agent-ui-chip").map((chip) => chip.dataset.key),
    ["build", "extension"],
    "an empty status text clears its key"
  );

  view.handle({
    type: "agent_event",
    event: { event: "widget.updated", key: "todo", text: "" },
  });
  assert.strictEqual(above.hidden, true, "an emptied widget disappears");

  view.handle({
    type: "agent_event",
    event: { event: "title.updated", title: "live title" },
  });
  assert.strictEqual(title.textContent, "live title", "title.updated renames the view");

  // Prefill only fills an empty composer.
  const input = findByClass(view.element, "pi-input");
  input.value = "";
  view.handle({ type: "agent_event", event: { event: "editor.prefill", text: "start here" } });
  assert.strictEqual(input.value, "start here", "a prefill fills an empty composer");
  input.value = "my draft";
  view.handle({ type: "agent_event", event: { event: "editor.prefill", text: "replaced" } });
  assert.strictEqual(input.value, "my draft", "a prefill never overwrites a draft");

  // A resync is authoritative: keys the snapshot no longer carries are gone.
  view.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: {},
    ui: { status: { only: "one" }, widgets: {}, title: "" },
    dialogs: [],
    cursor: { seq: 1 },
  });
  assert.deepStrictEqual(
    byClassAll(view.element, "agent-ui-chip").map((chip) => chip.textContent),
    ["one"],
    "a resync replaces the chip set"
  );
  assert.strictEqual(below.hidden, true, "a resync clears widgets");
  assert.strictEqual(title.textContent, "fallback title", "an empty title falls back");

  console.log("agent ui smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
