/* Minimal DOM shim + smoke test for webterm/static/app.js.
 *
 * The interesting regression here is focus: the server pushes a fresh session
 * list whenever a structured session changes state (and streaming agents do
 * that while answering), so the reconciler must not re-run activation and pull
 * the caret back into the active composer on every metadata-only update.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const focused = [];

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this._classes = new Set();
    this._listeners = Object.create(null);
    this.attributes = Object.create(null);
    this.dataset = {};
    this.style = {
      setProperty(name, value) {
        this[name] = value;
      },
    };
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.placeholder = "";
    this.rows = 0;
    this.type = "";
    this.title = "";
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.width = 220;
    this.height = 100;
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

  get parentNode() {
    return this.parent;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  set innerHTML(value) {
    if (value === "") this.children = [];
  }

  get innerHTML() {
    return "";
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  appendChild(node) {
    if (!node) return node;
    this.children.push(node);
    if (typeof node === "object") node.parent = this;
    return node;
  }

  prepend(node) {
    this.children.unshift(node);
    if (node && typeof node === "object") node.parent = this;
  }

  replaceChild(newNode, oldNode) {
    const index = this.children.indexOf(oldNode);
    if (index >= 0) this.children[index] = newNode;
    if (newNode && typeof newNode === "object") newNode.parent = this;
    if (oldNode && typeof oldNode === "object") oldNode.parent = null;
  }

  replaceChildren() {
    this.children = [];
  }

  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
      this.parent = null;
    }
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  focus() {
    focused.push(this);
  }

  select() {}

  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child && child.contains && child.contains(node));
  }

  closest() {
    return null;
  }

  getBoundingClientRect() {
    return {
      width: this.width,
      height: this.height,
      left: 0,
      top: 0,
      right: this.width,
      bottom: this.height,
    };
  }

  addEventListener(type, handler) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(handler);
  }

  dispatch(type, event) {
    for (const handler of this._listeners[type] || []) {
      handler(event || { preventDefault() {}, stopPropagation() {}, button: 0, pointerType: "mouse" });
    }
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const value = String(selector || "");
    const className = value.startsWith(".") ? value.slice(1) : null;
    const attribute = value.startsWith("[") ? value.slice(1, -1) : null;
    const tagName = className || attribute ? null : value.toUpperCase();
    const found = [];
    const walk = (node) => {
      if (node && node._classes) {
        const matches = className
          ? node._classes.has(className)
          : attribute
            ? attribute in node.dataset
            : node.tagName === tagName;
        if (matches) found.push(node);
      }
      for (const child of (node && node.children) || []) {
        if (child && typeof child === "object") walk(child);
      }
    };
    walk(this);
    return found;
  }
}

const byId = new Map();
function elementFor(id) {
  if (!byId.has(id)) byId.set(id, new FakeElement("div"));
  return byId.get(id);
}

const storage = new Map();
global.window = global;
global.Element = FakeElement;
global.document = {
  body: new FakeElement("body"),
  documentElement: new FakeElement("html"),
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elementFor(id),
  addEventListener() {},
};
global.localStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};
global.location = {
  protocol: "http:",
  host: "127.0.0.1:8765",
  search: "",
  pathname: "/",
  hash: "",
};
global.history = { replaceState() {} };
global.innerWidth = 1024;
global.innerHeight = 800;
global.requestAnimationFrame = (callback) => {
  callback();
  return 1;
};
global.alert = () => {};
global.confirm = () => true;
global.WEBTERM_CONFIG = {};
global.Terminal = function Terminal() {};
global.WebtermHighlight = null;

// Every focus() call lands here; the app's global listeners are irrelevant to
// this test but must exist so loading does not throw.
global.addEventListener = () => {};

class FakeWebSocket {
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.sent = [];
    FakeWebSocket.last = this;
  }

  send(data) {
    this.sent.push(data);
  }

  close() {}
}

global.WebSocket = FakeWebSocket;

require(path.join(__dirname, "..", "webterm", "static", "agent.js"));
require(path.join(__dirname, "..", "webterm", "static", "app.js"));

const socket = FakeWebSocket.last;
assert.ok(socket, "app.js opened a WebSocket");
socket.onopen();

function deliver(message) {
  socket.onmessage({ data: JSON.stringify(message) });
}

function session(id, overrides) {
  return {
    id,
    title: id,
    kind: "agent",
    adapter: "llama",
    alive: true,
    streaming: false,
    settled: true,
    ...(overrides || {}),
  };
}

function inputs() {
  return byId.get("terminal-area").querySelectorAll(".pi-input");
}

deliver({ type: "sessions", sessions: [session("1-1")] });
assert.strictEqual(focused.length, 1, "the first session focuses its composer");
assert.strictEqual(focused[0], inputs()[0], "the focused element is the composer");

// Metadata-only updates arrive while the agent is answering; they must not
// pull focus back into the composer.
focused.length = 0;
deliver({
  type: "sessions",
  sessions: [session("1-1", { streaming: true, activity: "answering", tokens: 7, seq: 12 })],
});
assert.deepStrictEqual(focused, [], "a streaming update does not steal focus");

// A sub-agent tab appearing must not steal focus from the active view either.
deliver({
  type: "sessions",
  sessions: [
    session("1-1", { streaming: true, tokens: 9, seq: 20 }),
    session("2-1", { parent_id: "1-1", role: "agent", title: "child" }),
  ],
});
assert.deepStrictEqual(focused, [], "a new child tab does not steal focus");
assert.strictEqual(inputs().length, 2, "the child view exists");

// Switching tabs is an explicit activation and does move the caret.
focused.length = 0;
const tabs = byId.get("sessions").children;
assert.strictEqual(tabs.length, 2, "one tab per session");
tabs[1].dispatch("click");
assert.strictEqual(focused.length, 1, "clicking a tab focuses its composer");
assert.strictEqual(focused[0], inputs()[1], "focus follows the chosen tab");

// Returning to the first tab works the same way.
focused.length = 0;
tabs[0].dispatch("click");
assert.strictEqual(focused.length, 1, "clicking the first tab focuses it again");
assert.strictEqual(focused[0], inputs()[0], "focus returns to the first composer");

console.log("app view focus test passed");
