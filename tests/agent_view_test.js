/* Minimal DOM shim + smoke test for webterm/static/agent.js. */
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

  setAttribute() {}
  focus() {}

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  dispatch(type, event) {
    const handler = this._listeners[type];
    if (handler) handler(event || { preventDefault() {} });
  }

  querySelectorAll(selector) {
    const value = String(selector || "");
    const className = value.startsWith(".") ? value.slice(1) : null;
    const tagName = className ? null : value.toUpperCase();
    const found = [];
    const walk = (node) => {
      if (node && node._classes) {
        if (className ? node._classes.has(className) : node.tagName === tagName) {
          found.push(node);
        }
      }
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

const calls = [];
const view = window.WebtermAgent.create(
  { id: "1-1", title: "agent smoke", model: "smoke-model" },
  {
    onPrompt: (text, streamingBehavior) => {
      calls.push(["prompt", text, streamingBehavior]);
      return Promise.resolve();
    },
    onAbort: () => {
      calls.push(["abort"]);
      return Promise.resolve();
    },
    onDialogResponse: (id, kind, response) => {
      calls.push(["dialog", id, kind, response]);
      return Promise.resolve();
    },
  }
);

assert.ok(view && view.element, "view element exists");

view.handle({
  type: "agent_resync",
  entries: [
    { id: "e1", role: "user", content: [{ type: "text", text: "hello" }] },
  ],
  state: { model: "smoke-model", streaming: false, settled: true, tokens: 12 },
  capabilities: {
    prompt: true,
    abort: true,
    models: { list: true },
    dialogs: ["confirm"],
  },
  dialogs: [],
  cursor: { seq: 4 },
});
assert.ok(textOf(view.element).includes("hello"), "snapshot entry rendered");
assert.ok(textOf(view.element).includes("12 tok"), "usage status rendered");

view.handle({
  type: "agent_event",
  event: {
    event: "message.started",
    message: { id: "m1", role: "assistant", content: [] },
  },
});
view.handle({
  type: "agent_event",
  event: {
    event: "message.delta",
    message_id: "m1",
    block_index: 0,
    block_type: "text",
    delta: "partial",
  },
});
assert.ok(textOf(view.element).includes("partial"), "live delta rendered");
view.handle({
  type: "agent_event",
  event: {
    event: "message.finished",
    message: {
      id: "m1",
      role: "assistant",
      content: [{ type: "text", text: "partial answer" }],
    },
  },
});
assert.ok(textOf(view.element).includes("partial answer"), "finished message rendered");

// A replayed event at or below the snapshot cursor is already in the snapshot.
view.handle({
  type: "agent_event",
  seq: 2,
  event: {
    event: "message.finished",
    message: {
      id: "stale",
      role: "assistant",
      content: [{ type: "text", text: "stale replay" }],
    },
  },
});
assert.ok(!textOf(view.element).includes("stale replay"), "stale replay dropped");
view.handle({
  type: "agent_event",
  seq: 99,
  event: {
    event: "tool.started",
    call_id: "t1",
    name: "bash",
    args: { command: "ls" },
  },
});
view.handle({
  type: "agent_event",
  event: {
    event: "message.block",
    message_id: "m1",
    block_index: 1,
    block: {
      type: "tool_call",
      call_id: "t1",
      name: "bash",
      args: { command: "ls" },
    },
  },
});
assert.ok(textOf(view.element).includes("bash"), "tool card rendered");

view.handle({
  type: "agent_dialog_request",
  request: {
    type: "dialog_request",
    id: "d1",
    kind: "confirm",
    title: "Proceed?",
    message: "sure?",
  },
});
const dialog = findByClass(view.element, "agent-dialog");
assert.ok(dialog, "dialog panel rendered");
const approve = dialog.querySelectorAll("button").find((b) => b.textContent === "Approve");
assert.ok(approve, "approve button rendered");
approve.dispatch("click");
assert.deepStrictEqual(calls[calls.length - 1], [
  "dialog",
  "d1",
  "confirm",
  { approved: true },
]);
view.handle({ type: "agent_dialog_resolved", dialog_id: "d1", reason: "answered" });
assert.strictEqual(findByClass(view.element, "agent-dialog"), null, "dialog removed");

const input = findByClass(view.element, "pi-input");
const form = findByClass(view.element, "pi-composer");
input.value = "next prompt";
form.dispatch("submit");
assert.deepStrictEqual(calls[calls.length - 1], ["prompt", "next prompt", undefined]);
assert.ok(textOf(view.element).includes("next prompt"), "optimistic prompt rendered");

// A prompt during a run must say how to queue it, or the adapter rejects it.
view.handle({
  type: "agent_event",
  event: { event: "run.started", run_id: "r1" },
});
input.value = "queued follow-up";
form.dispatch("submit");
assert.deepStrictEqual(calls[calls.length - 1], [
  "prompt",
  "queued follow-up",
  "followUp",
]);
input.value = "queued steer";
input.dispatch("keydown", {
  key: "Enter",
  ctrlKey: true,
  preventDefault() {},
});
assert.deepStrictEqual(calls[calls.length - 1], [
  "prompt",
  "queued steer",
  "steer",
]);

view.setExited();
assert.ok(textOf(view.element).includes("exited"), "exit status rendered");

// Backwards paging: the view asks for the page before its first entry.
const pagedCalls = [];
const paged = window.WebtermAgent.create(
  { id: "2-1", title: "paged" },
  {
    onHistory: (before, limit) => {
      pagedCalls.push([before, limit]);
      return Promise.resolve({
        entries: [
          { id: "e0", role: "user", content: [{ type: "text", text: "older message" }] },
        ],
        has_more: false,
        first_entry_id: "e0",
      });
    },
  }
);
paged.handle({
  type: "agent_resync",
  entries: [{ id: "e1", role: "assistant", content: [{ type: "text", text: "newest" }] }],
  state: { streaming: false, settled: true },
  capabilities: { prompt: true, abort: true, history_paging: true },
  has_more: true,
  first_entry_id: "e1",
  cursor: { seq: 3 },
});
assert.ok(
  findByClass(paged.element, "pi-history-more"),
  "load-earlier button rendered when the snapshot has older entries"
);
findByClass(paged.element, "pi-history-more").dispatch("click");
assert.deepStrictEqual(pagedCalls, [["e1", 40]], "page requested before the oldest entry");

(async () => {
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(textOf(paged.element).includes("older message"), "earlier page prepended");
  assert.strictEqual(
    findByClass(paged.element, "pi-history-more"),
    null,
    "load-earlier button gone once the history is exhausted"
  );
  console.log("agent view smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
