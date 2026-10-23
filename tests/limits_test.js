/* Minimal DOM shim + smoke test for the pi view's `/limits` disclosure.
 *
 * Drives webterm/static/pi.js far enough to send a draft through the composer,
 * so the test observes the same path a user does: palette listing, the direct
 * `/trust` refusal, and the modal that names the TUI-only surfaces.
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
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.title = "";
    this.placeholder = "";
    this.rows = 0;
    this.tabIndex = 0;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this._listeners = Object.create(null);
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
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name);
        else classes.delete(name);
        return enabled;
      },
    };
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  focus() {}

  setSelectionRange() {}

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

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  querySelector(selector) {
    const matches = this.querySelectorAll(selector);
    return matches.length ? matches[0] : null;
  }

  querySelectorAll(selector) {
    const value = String(selector || "");
    const parts = value.split(".").filter(Boolean);
    const found = [];
    walk(this, (node) => {
      if (!node || !node._classes) return;
      if (parts.every((name) => node._classes.has(name))) found.push(node);
    });
    return found;
  }

  dispatch(type, event) {
    const handler = this._listeners[type];
    const synthetic = event || {};
    synthetic.preventDefault = synthetic.preventDefault || (() => {});
    synthetic.stopPropagation = synthetic.stopPropagation || (() => {});
    if (handler) handler(synthetic);
  }
}

global.document = {
  createElement: (tag) => new FakeNode(tag),
  body: new FakeNode("body"),
  addEventListener() {},
  removeEventListener() {},
};
global.window = global;
global.requestAnimationFrame = (callback) => callback();
global.WebtermHighlight = null;

require(path.join(__dirname, "..", "webterm", "static", "pi.js"));

function walk(node, visit) {
  visit(node);
  for (const child of (node && node.children) || []) {
    if (child && typeof child === "object") walk(child, visit);
  }
}

function byClass(root, className) {
  const found = [];
  walk(root, (node) => {
    if (node && node._classes && node._classes.has(className)) found.push(node);
  });
  return found;
}

function textOf(node) {
  let text = node.textContent || "";
  for (const child of node.children || []) text += textOf(child);
  return text;
}

const calls = [];
const view = window.WebtermPi.create(
  { id: "1-1", title: "limits" },
  {
    onCommand: (command) => {
      calls.push(command);
      return Promise.resolve({});
    },
    onHistory: () => Promise.resolve({ entries: [], hasMore: false }),
  }
);

view.handle({
  type: "pi_resync",
  seq: 0,
  streaming: false,
  state: {},
  capabilities: {},
  commands: { commands: [] },
  ui: {},
  entries: { entries: [], hasMore: false },
});

const input = byClass(view.element, "pi-input")[0];
assert.ok(input, "the composer rendered");

// ------------------------------------------------------------------- palette
input.value = "/lim";
input.dispatch("input");
const palette = byClass(view.element, "pi-command-palette")[0];
const paletteText = textOf(palette);
assert.ok(paletteText.includes("limits"), "`/limits` is offered in the palette");
assert.ok(
  paletteText.includes("cannot do"),
  "the palette entry says what it is for"
);

// --------------------------------------------------------------------- modal
input.value = "/limits";
input.dispatch("keydown", { key: "Enter" });
const overlays = byClass(document.body, "pi-modal-overlay");
assert.strictEqual(overlays.length, 1, "`/limits` opens a modal");
const modalText = textOf(overlays[0]);
for (const name of ["/login", "/settings", "/trust", "/share", "/bug", "ctx.ui.custom()"]) {
  assert.ok(modalText.includes(name), "the modal names " + name);
}
assert.ok(
  modalText.includes("pi agent launcher"),
  "the modal points at the PTY escape hatch"
);
assert.deepStrictEqual(calls, [], "`/limits` sends nothing to pi");
overlays[0].remove();

// ------------------------------------------------------- direct TUI-only slash
input.value = "/trust";
input.dispatch("keydown", { key: "Enter" });
assert.deepStrictEqual(calls, [], "a TUI-only command is never forwarded as a prompt");
const banner = byClass(view.element, "pi-banner")[0];
assert.ok(
  textOf(banner).includes("TUI-only"),
  "the refusal explains itself instead of failing silently"
);
assert.strictEqual(input.value, "/trust", "the draft is kept so it can be edited");

// ------------------------------------------------------------ normal prompts
input.value = "hello pi";
input.dispatch("keydown", { key: "Enter" });
assert.deepStrictEqual(calls, [{ type: "prompt", message: "hello pi" }], "prompts still send");

console.log("limits smoke test passed");
