/* Minimal DOM shim + smoke test for webterm/static/find.js. */
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
    this.placeholder = "";
    this.title = "";
    this._listeners = Object.create(null);
    this.focusCount = 0;
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

  focus() {
    this.focusCount += 1;
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
      for (const child of (node && node.children) || []) walk(child);
    };
    walk(this);
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

global.document = { createElement: (tag) => new FakeNode(tag) };
global.window = global;

require(path.join(__dirname, "..", "webterm", "static", "find.js"));

function textOf(node) {
  let text = node.textContent || "";
  for (const child of node.children || []) text += textOf(child);
  return text;
}

function byClass(root, className) {
  const found = [];
  const walk = (node) => {
    if (node && node._classes && node._classes.has(className)) found.push(node);
    for (const child of (node && node.children) || []) walk(child);
  };
  walk(root);
  return found;
}

function openBar(opts) {
  const host = new FakeNode("div");
  const calls = { reveal: [], loaded: 0 };
  const finder = window.WebtermFind.create(host, {
    items: opts.items,
    reveal: (id) => calls.reveal.push(id),
    hasMore: opts.hasMore,
    loadEarlier: opts.loadEarlier
      ? () => {
          calls.loaded += 1;
          return opts.loadEarlier(calls.loaded);
        }
      : undefined,
    focusTarget: opts.focusTarget,
  });
  return { host, finder, calls };
}

function type(finder, host, value) {
  const input = byClass(host, "find-input")[0];
  input.value = value;
  input.dispatch("input");
  return input;
}

// ------------------------------------------------------------------ matching
const entries = [
  { id: "e1", role: "user", text: "please run the tests for the parser" },
  { id: "e2", role: "assistant", text: "I ran the tests: 12 passed." },
  { id: "e3", role: "tool", text: "pytest output, no matches here" },
];
{
  const { host, finder, calls } = openBar({ items: () => entries });
  assert.strictEqual(byClass(host, "find-bar")[0].hidden, true, "bar starts hidden");
  finder.open();
  assert.strictEqual(byClass(host, "find-bar")[0].hidden, false, "open shows the bar");

  type(finder, host, "tests");
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "1 / 2", "case-insensitive count");
  assert.deepStrictEqual(calls.reveal, ["e1"], "the first match is revealed immediately");
  assert.strictEqual(byClass(host, "find-role")[0].textContent, "user", "the snippet names the role");
  const mark = byClass(host, "find-snippet")[0].children.find((node) => node.tagName === "MARK");
  assert.strictEqual(textOf(mark), "tests", "the hit is marked inside the snippet");

  finder.step(1);
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "2 / 2");
  assert.deepStrictEqual(calls.reveal, ["e1", "e2"], "next reveals the following entry");
  finder.step(1);
  assert.deepStrictEqual(calls.reveal, ["e1", "e2", "e1"], "next wraps around");
  finder.step(-1);
  assert.deepStrictEqual(calls.reveal, ["e1", "e2", "e1", "e2"], "previous wraps backwards");

  type(finder, host, "nothing-here");
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "no matches");
  assert.deepStrictEqual(calls.reveal.length, 4, "a miss reveals nothing");
  assert.strictEqual(byClass(host, "find-snippet")[0].children.length, 0, "no snippet on a miss");
}

// -------------------------------------------------------------------- keyboard
{
  const target = new FakeNode("textarea");
  const { host, finder } = openBar({
    items: () => entries,
    focusTarget: () => target,
  });
  finder.open();
  const input = type(finder, host, "tests");
  input.dispatch("keydown", { key: "Enter" });
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "2 / 2", "Enter advances");
  input.dispatch("keydown", { key: "Enter", shiftKey: true });
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "1 / 2", "Shift+Enter goes back");
  input.dispatch("keydown", { key: "Escape" });
  assert.strictEqual(byClass(host, "find-bar")[0].hidden, true, "Escape closes the bar");
  assert.strictEqual(target.focusCount, 1, "closing returns focus to the view");
}

// -------------------------------------------------------------- earlier pages
(async () => {
  let searchable = [{ id: "e9", role: "user", text: "loaded page" }];
  const { host, finder } = openBar({
    items: () => searchable,
    hasMore: () => true,
    loadEarlier: () =>
      Promise.resolve().then(() => {
        searchable = [
          { id: "e1", role: "user", text: "an older question about widgets" },
          ...searchable,
        ];
        return true;
      }),
  });
  finder.open();
  type(finder, host, "widgets");
  const more = byClass(host, "find-more")[0];
  assert.strictEqual(byClass(host, "find-count")[0].textContent, "no matches");
  assert.strictEqual(more.hidden, false, "an unloaded page offers to search earlier entries");
  more.dispatch("click");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.strictEqual(
    byClass(host, "find-count")[0].textContent,
    "1 / 1",
    "the page was searched again"
  );
  assert.strictEqual(more.hidden, true, "the offer disappears once a match exists");
  console.log("find smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

// ------------------------------------------------------------ view wiring
global.requestAnimationFrame = (callback) => callback();
global.WebtermHighlight = null;
require(path.join(__dirname, "..", "webterm", "static", "agent.js"));

(async () => {
  const view = window.WebtermAgent.create(
    { id: "9-1", title: "find" },
    {
      onCommand: () => Promise.resolve({}),
      onAbort: () => Promise.resolve({}),
      onHistory: () =>
        Promise.resolve({ entries: [], has_more: false, first_entry_id: "" }),
    }
  );
  view.handle({
    type: "agent_resync",
    entries: [
      { id: "e1", role: "user", content: [{ type: "text", text: "first question" }] },
      {
        id: "e2",
        role: "assistant",
        content: [
          { type: "thinking", text: "hidden deliberation about widgets" },
          { type: "text", text: "first answer" },
        ],
      },
      { id: "e3", role: "tool", content: [{ type: "text", text: "tool output" }] },
    ],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, abort: true },
    dialogs: [],
    cursor: { seq: 0 },
  });

  const button = byClass(view.element, "pi-find-toggle")[0];
  assert.ok(button, "the agent view has a find button");
  assert.strictEqual(button.hidden, false, "the button is shown without the finder being open");
  button.dispatch("click");
  assert.strictEqual(byClass(view.element, "find-bar")[0].hidden, false, "the button opens the bar");

  const input = byClass(view.element, "find-input")[0];
  input.value = "widgets";
  input.dispatch("input");
  assert.strictEqual(
    byClass(view.element, "find-count")[0].textContent,
    "1 / 1",
    "matching runs over the rendered entry text"
  );
  const article = byClass(view.element, "agent-msg").find(
    (node) => node.dataset.entryId === "e2"
  );
  assert.ok(article, "transcript entries carry their id");
  assert.strictEqual(article.classList.contains("find-hit"), true, "the matching entry is outlined");
  const thinking = byClass(article, "agent-thinking")[0];
  assert.ok(thinking, "the thinking block rendered");
  assert.strictEqual(thinking.open, true, "a collapsed block opens so the match is visible");
  console.log("find view wiring passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
