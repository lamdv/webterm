/* Minimal DOM shim + smoke test for webterm/static/pathcomplete.js. */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this._classes = new Set();
    this._listeners = Object.create(null);
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.title = "";
    this.value = "";
    this.dataset = {};
    this.selectionStart = 0;
    this.selectionEnd = 0;
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
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
      this.parent = null;
    }
  }

  setAttribute() {}
  focus() {}
  scrollIntoView() {}

  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }

  addEventListener(type, handler) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(handler);
  }

  removeEventListener(type, handler) {
    const handlers = this._listeners[type];
    if (!handlers) return;
    this._listeners[type] = handlers.filter((item) => item !== handler);
  }

  dispatch(type, event) {
    const payload = event || {};
    if (typeof payload.preventDefault !== "function") payload.preventDefault = () => {};
    for (const handler of (this._listeners[type] || []).slice()) handler(payload);
  }
}

global.document = {
  createElement: (tag) => new FakeNode(tag),
};
global.window = global;
global.requestAnimationFrame = (callback) => callback();

require(path.join(__dirname, "..", "webterm", "static", "pathcomplete.js"));

const { tokenAt, splitQuery, resolveDir, quotePath } = window.WebtermPathComplete;

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

function findItem(root, className) {
  for (const child of root.children) {
    if (child && child._classes && child._classes.has(className)) return child;
  }
  return null;
}

function makeInput(initial) {
  const input = new FakeNode("textarea");
  input.value = initial || "";
  input.selectionStart = input.value.length;
  return input;
}

// --- Token detection -------------------------------------------------------

assert.deepStrictEqual(tokenAt("hi @sr", 6), { start: 3, end: 6, query: "sr" });
assert.deepStrictEqual(tokenAt("@", 1), { start: 0, end: 1, query: "" });
assert.deepStrictEqual(tokenAt("line\n@a/b", 9), { start: 5, end: 9, query: "a/b" });
assert.strictEqual(tokenAt("mail@host", 9), null, "no boundary before @");
assert.strictEqual(tokenAt("a @bc", 4), null, "caret inside a token");
assert.strictEqual(tokenAt("a @b", 2), null, "caret before the token");
assert.strictEqual(tokenAt("plain", 5), null);
assert.strictEqual(tokenAt("", 0), null);

// --- Query shaping ---------------------------------------------------------

assert.deepStrictEqual(splitQuery(""), { dir: "", prefix: "" });
assert.deepStrictEqual(splitQuery("src/pi"), { dir: "src/", prefix: "pi" });
assert.deepStrictEqual(splitQuery("/a/b/"), { dir: "/a/b/", prefix: "" });
assert.strictEqual(resolveDir("/work", ""), "/work");
assert.strictEqual(resolveDir("/work/", "src/"), "/work/src/");
assert.strictEqual(resolveDir("/work", "/abs/"), "/abs/");
assert.strictEqual(resolveDir("", "src/"), "src/");
assert.strictEqual(quotePath("/work/a.txt"), "/work/a.txt");
assert.strictEqual(quotePath('/work/a "b" $c.txt'), '"/work/a \\"b\\" \\$c.txt"');

// --- Picker behaviour ------------------------------------------------------

const listings = {
  "/work": {
    path: "/work",
    entries: [
      { name: "src", path: "/work/src", type: "dir" },
      { name: "a.txt", path: "/work/a.txt", type: "file" },
      { name: "ab.md", path: "/work/ab.md", type: "file" },
      { name: "broken\nname", path: "/work/broken", type: "file" },
    ],
  },
  "/work/src/": {
    path: "/work/src",
    entries: [
      { name: "pi.js", path: "/work/src/pi.js", type: "file" },
      { name: "my file.txt", path: "/work/src/my file.txt", type: "file" },
    ],
  },
};
const requests = [];
const sends = [];
const input = makeInput("");
const picker = window.WebtermPathComplete.create({
  input: input,
  basePath: "/work",
  listEntries: (target) => {
    requests.push(target);
    return Promise.resolve(listings[target] || { path: target, entries: [] });
  },
});
// A composer sends on Enter unless the picker consumed the key first.
input.addEventListener("keydown", (event) => {
  if (picker.keydown(event)) return;
  if (event.key === "Enter" && !event.shiftKey) sends.push(input.value.trim());
});

function key(name, extra, target) {
  const event = Object.assign({ key: name, defaultPrevented: false }, extra || {});
  event.preventDefault = () => {
    event.defaultPrevented = true;
  };
  (target || input).dispatch("keydown", event);
  return event;
}

async function type(text, caret) {
  input.value = text;
  input.selectionStart = caret === undefined ? text.length : caret;
  input.dispatch("input");
  await tick();
}

function activeName() {
  const item = findItem(picker.element, "active");
  const name = item && findItem(item, "path-complete-name");
  return name ? name.textContent : null;
}

(async () => {
  assert.ok(picker.element.hidden, "picker starts hidden");

  await type("@");
  assert.deepStrictEqual(requests, ["/work"], "bare @ lists the base directory");
  assert.ok(picker.isOpen(), "picker opens for @");
  assert.strictEqual(activeName(), "src/");
  assert.strictEqual(picker.element.children.length, 3, "newline name is not offered");

  await type("@a");
  assert.strictEqual(picker.element.children.length, 2, "typing filters the listing");
  assert.strictEqual(activeName(), "a.txt");

  // Arrow keys move the highlight, wrapping at the ends.
  key("ArrowDown");
  assert.strictEqual(activeName(), "ab.md");
  key("ArrowDown");
  assert.strictEqual(activeName(), "a.txt");
  key("ArrowUp");
  assert.strictEqual(activeName(), "ab.md");

  // Enter accepts the highlighted file and must not send the prompt.
  key("Enter");
  assert.strictEqual(input.value, "/work/ab.md", "accept inserts the literal path");
  assert.strictEqual(sends.length, 0, "Enter with the picker open does not send");
  assert.ok(picker.element.hidden, "accept closes the picker");

  // The next Enter, with the picker closed, does send.
  key("Enter");
  assert.deepStrictEqual(sends, ["/work/ab.md"]);

  // @dir/ descends: the request goes to the directory and the names filter.
  await type("@src/");
  assert.deepStrictEqual(requests[requests.length - 1], "/work/src/");
  assert.strictEqual(picker.element.children.length, 2, "directory entries listed");

  // Accepting a directory keeps the @ so the picker can descend further.
  await type("@s");
  assert.strictEqual(activeName(), "src/");
  key("Enter");
  assert.strictEqual(input.value, "@/work/src/", "directory accept keeps the token");
  await tick();
  assert.ok(picker.isOpen(), "directory accept stays open");
  assert.strictEqual(activeName(), "pi.js");

  // Escape closes only the picker and leaves the text alone.
  const before = input.value;
  const escape = key("Escape");
  assert.ok(escape.defaultPrevented, "Escape is consumed");
  assert.ok(picker.element.hidden, "Esc closes the picker");
  assert.strictEqual(input.value, before, "Esc does not touch the draft");

  // A path with whitespace is inserted quoted, and Shift+Enter is not accepted.
  await type("@src/my");
  assert.strictEqual(activeName(), "my file.txt");
  const shiftEnter = key("Enter", { shiftKey: true });
  assert.ok(!shiftEnter.defaultPrevented, "Shift+Enter stays a newline");
  assert.ok(picker.isOpen(), "Shift+Enter leaves the picker open");
  key("Tab");
  assert.strictEqual(input.value, '"/work/src/my file.txt"', "spaces are quoted");

  // A failed listing closes the picker instead of surfacing an error.
  const failing = window.WebtermPathComplete.create({
    input: input,
    basePath: "/work",
    listEntries: () => Promise.reject(new Error("nope")),
  });
  await type("@x");
  assert.ok(failing.element.hidden, "a rejected listing hides the picker");

  // Closing must not reopen from a listing that resolves later.
  let release = null;
  const slow = window.WebtermPathComplete.create({
    input: input,
    basePath: "/work",
    listEntries: () =>
      new Promise((resolve) => {
        release = resolve;
      }),
  });
  await type("@slow");
  slow.close();
  release(listings["/work"]);
  await tick();
  assert.ok(slow.element.hidden, "a late listing cannot reopen a closed picker");

  failing.destroy();
  slow.destroy();
  picker.destroy();
  assert.ok(!input._listeners.input || input._listeners.input.length === 0);

  // --- Composer wiring -----------------------------------------------------

  require(path.join(__dirname, "..", "webterm", "static", "pi.js"));
  require(path.join(__dirname, "..", "webterm", "static", "agent.js"));

  function findByClass(root, className) {
    for (const child of (root && root.children) || []) {
      if (!child || typeof child !== "object") continue;
      if (child._classes && child._classes.has(className)) return child;
      const nested = findByClass(child, className);
      if (nested) return nested;
    }
    return null;
  }

  const listing = {
    "/work": {
      path: "/work",
      entries: [{ name: "a.txt", path: "/work/a.txt", type: "file" }],
    },
  };
  const listHook = (target) =>
    Promise.resolve(listing[target] || { path: target, entries: [] });

  const commands = [];
  const piView = window.WebtermPi.create(
    { id: "1-1", title: "pi smoke", cwd: "/work" },
    {
      onCommand: (command) => {
        commands.push(command);
        return Promise.resolve({ success: true });
      },
      onListEntries: listHook,
    }
  );
  const piInput = findByClass(piView.element, "pi-input");
  assert.ok(piInput, "pi composer input found");
  const piPicker = findByClass(piView.element, "path-complete");
  assert.ok(piPicker, "pi composer mounts the picker");
  piInput.value = "@";
  piInput.selectionStart = 1;
  piInput.dispatch("input");
  await tick();
  assert.strictEqual(piPicker.hidden, false, "pi picker opened");
  key("Enter", null, piInput);
  assert.strictEqual(piInput.value, "/work/a.txt", "pi accept inserts the path");
  assert.strictEqual(commands.length, 0, "pi Enter with the picker open does not send");
  key("Enter", null, piInput);
  assert.strictEqual(commands.length, 1, "pi Enter with the picker closed sends");
  assert.strictEqual(commands[0].type, "prompt");
  assert.strictEqual(commands[0].message, "/work/a.txt");
  piView.destroy();

  const prompts = [];
  const agentView = window.WebtermAgent.create(
    { id: "1-2", title: "agent smoke", cwd: "/work" },
    {
      onPrompt: (text) => {
        prompts.push(text);
        return Promise.resolve();
      },
      onAbort: () => Promise.resolve(),
      onCommand: () => Promise.resolve({}),
      onDialogResponse: () => Promise.resolve(),
      onListEntries: listHook,
    }
  );
  const agentInput = findByClass(agentView.element, "pi-input");
  assert.ok(agentInput, "canonical composer input found");
  const agentPicker = findByClass(agentView.element, "path-complete");
  assert.ok(agentPicker, "canonical composer mounts the picker");
  agentInput.value = "@";
  agentInput.selectionStart = 1;
  agentInput.dispatch("input");
  await tick();
  assert.strictEqual(agentPicker.hidden, false, "canonical picker opened");
  agentInput.dispatch("keydown", { key: "Enter", shiftKey: false, preventDefault() {} });
  assert.strictEqual(agentInput.value, "/work/a.txt", "canonical accept inserts the path");
  assert.strictEqual(prompts.length, 0, "canonical Enter with the picker open does not send");
  agentInput.dispatch("keydown", { key: "Enter", shiftKey: false, preventDefault() {} });
  assert.deepStrictEqual(prompts, ["/work/a.txt"], "canonical Enter then sends");
  agentView.destroy();

  console.log("pathcomplete: all assertions passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
