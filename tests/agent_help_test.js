/* Minimal DOM shim + smoke test for the canonical view's `/help` disclosure.
 *
 * The adapter declares the features the protocol cannot expose (`hello.help`);
 * the view must offer them as a palette entry and render them as a dialog,
 * without sending anything to the backend.  This is the canonical replacement
 * for the legacy view's `limits_test.js`, which was deleted with `pi.js` (#13):
 * the behaviour is the same, but the names and reasons arrive as data, so no
 * adapter vocabulary appears in `agent.js`.
 */
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

function byClassAll(root, className) {
  return root.querySelectorAll ? root.querySelectorAll("." + className) : [];
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

function type(node, value) {
  node.value = value;
  node.dispatch("input");
}

function press(node, key) {
  node.dispatch("keydown", {
    key,
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    preventDefault() {},
  });
}

/** The palette's visible entries as `{name, description}` pairs. */
function paletteEntries(view) {
  const palette = findByClass(view.element, "agent-palette");
  if (!palette || palette.hidden) return [];
  return byClassAll(palette, "agent-palette-item").map((item) => ({
    name: item.children[0].textContent,
    description: item.children
      .filter((child) => child && child.classList.contains("agent-palette-desc"))
      .map((child) => child.textContent)
      .join(""),
  }));
}

const calls = [];
const view = window.WebtermAgent.create(
  { id: "1-1", title: "help" },
  {
    onPrompt: (payload) => {
      calls.push(payload);
      return Promise.resolve();
    },
    onAbort: () => Promise.resolve(),
    onCommand: (command) => {
      calls.push(command);
      return Promise.resolve({});
    },
  }
);

const input = findByClass(view.element, "pi-input");
assert.ok(input, "the composer rendered");

// The adapter's declared gaps, plus no declared commands: `/help` is the only
// thing the palette can offer here.
view.handle({
  type: "agent_resync",
  entries: [],
  state: { streaming: false, settled: true },
  capabilities: { prompt: true },
  commands: [],
  help: [
    { name: "/settings", reason: "settings are edited elsewhere" },
    { name: "extension screens", reason: "" },
  ],
  ui: {},
  dialogs: [],
  cursor: { seq: 0 },
});

// ------------------------------------------------------------------- palette
type(input, "/he");
const entries = paletteEntries(view).filter((entry) => entry.name === "/help");
assert.strictEqual(entries.length, 1, "`help` is offered in the palette");
assert.ok(
  entries[0].description.length > 0,
  "the palette entry says what it is for"
);

// --------------------------------------------------------------------- modal
press(input, "Enter");
const help = findByClass(view.element, "agent-help");
assert.ok(help, "accepting `help` opens the dialog");
assert.strictEqual(help.hidden, false, "the dialog is visible");

const items = byClassAll(help, "agent-help-item");
assert.deepStrictEqual(
  items.map((item) => findByClass(item, "agent-help-name").textContent),
  ["/settings", "extension screens"],
  "every declared gap is listed, in adapter order"
);
assert.deepStrictEqual(
  items.map((item) => findByClass(item, "agent-help-reason").textContent.trim()),
  ["— settings are edited elsewhere", ""],
  "each gap carries its reason, and a missing one stays blank"
);
assert.ok(
  byClassAll(help, "agent-modal-close").length === 1,
  "the dialog can be dismissed"
);
assert.deepStrictEqual(calls, [], "`help` sends nothing to the backend");
assert.strictEqual(input.value, "", "the accepted draft is cleared, not sent");

// ------------------------------------------------- no declared gaps, no entry
view.handle({
  type: "agent_resync",
  entries: [],
  state: { streaming: false, settled: true },
  capabilities: { prompt: true },
  commands: [],
  help: [],
  ui: {},
  dialogs: [],
  cursor: { seq: 1 },
});
assert.ok(!findByClass(view.element, "agent-help"), "a resync closes the dialog");

type(input, "/he");
assert.deepStrictEqual(
  paletteEntries(view).filter((entry) => entry.name === "/help"),
  [],
  "without declared gaps there is no `help` entry to offer"
);

console.log("agent help smoke test passed");
