/* Minimal DOM shim + smoke test for the canonical view's queue chips and
 * slash-command palette (webterm/static/agent.js). */
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

(async () => {
  // ------------------------------------------------------------------ queue
  // A queue.updated event renders kind-labelled chips, the footer counts them,
  // and Clear sends clear_queue and puts the adapter's returned text back into
  // the composer.
  const calls = [];
  const longText = "long queued message ".repeat(12).trim();
  const view = window.WebtermAgent.create(
    { id: "q-1", title: "queued" },
    {
      onPrompt: (text, behavior) => {
        calls.push(["prompt", text, behavior]);
        return Promise.resolve();
      },
      onAbort: () => {
        calls.push(["abort"]);
        return Promise.resolve();
      },
      onCommand: (command) => {
        calls.push(["command", command]);
        if (command.type === "clear_queue") {
          return Promise.resolve({
            steering: ["alpha queued"],
            followUp: ["beta queued"],
          });
        }
        return Promise.resolve({});
      },
    }
  );
  view.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, abort: true, queue: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  const input = findByClass(view.element, "pi-input");
  const footer = findByClass(view.element, "agent-footer");
  const queue = findByClass(view.element, "agent-queue");

  // Escape while idle is still a no-op: it must not reach abort.
  keydown(input, "Escape");
  await tick();
  assert.deepStrictEqual(calls, [], "Escape while idle sends nothing");

  view.handle({
    type: "agent_event",
    event: {
      event: "queue.updated",
      steering: ["do alpha", "do beta", longText],
      follow_up: ["later gamma"],
    },
  });
  assert.strictEqual(queue.hidden, false, "queue chips are shown");
  const chips = queue.querySelectorAll(".agent-queue-chip");
  assert.strictEqual(chips.length, 4, "every queued item has a chip");
  assert.deepStrictEqual(
    chips.map((chip) => chip.children[0].textContent),
    ["steer", "steer", "steer", "follow-up"],
    "chips name the queue kind"
  );
  assert.ok(
    chips[2].children[1].textContent.length < longText.length,
    "a long queued message is truncated"
  );
  assert.ok(chips[2].children[1].textContent.endsWith("…"), "the truncation is marked");
  assert.strictEqual(chips[2].title, longText, "the full text stays in the title");
  assert.ok(textOf(footer).includes("4 queued"), "the footer counts queued input");

  findByClass(view.element, "agent-queue-clear").dispatch("click");
  await tick();
  assert.deepStrictEqual(
    calls.filter((call) => call[0] === "command"),
    [["command", { type: "clear_queue" }]],
    "Clear sends one clear_queue"
  );
  assert.strictEqual(
    input.value,
    "alpha queued\nbeta queued",
    "the returned queued text is restored into the composer"
  );
  assert.strictEqual(queue.hidden, true, "the chips disappear after clearing");
  assert.ok(!textOf(footer).includes("queued"), "the footer count is cleared");
  calls.length = 0;

  // Esc while busy clears first, restores the text, then aborts.
  view.handle({
    type: "agent_event",
    event: { event: "run.started", run_id: "r1" },
  });
  view.handle({
    type: "agent_event",
    event: {
      event: "queue.updated",
      steering: ["pending steer"],
      follow_up: ["pending follow"],
    },
  });
  input.value = "";
  keydown(input, "Escape");
  await tick();
  await tick();
  assert.deepStrictEqual(
    calls,
    [
      ["command", { type: "clear_queue" }],
      ["abort"],
    ],
    "Esc clears the queue before aborting"
  );
  assert.strictEqual(
    input.value,
    "alpha queued\nbeta queued",
    "Esc restores the queued text before aborting"
  );
  assert.strictEqual(queue.hidden, true, "Esc drops the chips");
  calls.length = 0;

  // The Stop button takes the same path.
  view.handle({
    type: "agent_event",
    event: { event: "run.started", run_id: "r2" },
  });
  view.handle({
    type: "agent_event",
    event: {
      event: "queue.updated",
      steering: ["stop steer"],
      follow_up: [],
    },
  });
  input.value = "";
  findByClass(view.element, "agent-stop").dispatch("click");
  await tick();
  await tick();
  assert.deepStrictEqual(
    calls,
    [
      ["command", { type: "clear_queue" }],
      ["abort"],
    ],
    "Stop clears the queue before aborting"
  );
  assert.strictEqual(
    input.value,
    "alpha queued\nbeta queued",
    "Stop restores the queued text"
  );

  // ---------------------------------------------------------------- palette
  // `/` opens a filterable palette of only the capabilities the adapter
  // advertises; Enter accepts and never sends the draft as a prompt.
  const paletteCalls = [];
  const prompts = [];
  const paletteView = window.WebtermAgent.create(
    { id: "p-1", title: "palette" },
    {
      onPrompt: (text) => {
        prompts.push(text);
        return Promise.resolve();
      },
      onAbort: () => Promise.resolve(),
      onCommand: (command) => {
        paletteCalls.push(command);
        if (command.type === "list_models") {
          return Promise.resolve({
            models: [
              { id: "model-a", name: "Model A" },
              { id: "model-b", name: "Model B" },
            ],
            default: "model-a",
          });
        }
        return Promise.resolve({});
      },
    }
  );
  paletteView.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: {
      prompt: true,
      abort: true,
      compaction: true,
      models: { list: true, set: true, thinking: true, levels: ["off", "high"] },
      system_prompt: true,
    },
    dialogs: [],
    cursor: { seq: 0 },
  });
  const paletteInput = findByClass(paletteView.element, "pi-input");
  const paletteEl = findByClass(paletteView.element, "agent-palette");
  const modelSelect = findByClass(paletteView.element, "agent-model");
  const thinkingSelect = findByClass(paletteView.element, "agent-thinking");
  const systemPanel = findByClass(paletteView.element, "agent-system-panel");
  const systemInput = findByClass(paletteView.element, "agent-system-input");

  type(paletteInput, "/");
  assert.strictEqual(paletteEl.hidden, false, "`/` opens the palette");
  assert.deepStrictEqual(
    paletteNames(paletteView),
    ["/compact", "/model", "/thinking", "/system"],
    "the palette lists the capability-enabled commands"
  );

  type(paletteInput, "/mo");
  assert.deepStrictEqual(paletteNames(paletteView), ["/model"], "the palette filters by prefix");

  keydown(paletteInput, "Enter");
  assert.deepStrictEqual(prompts, [], "Enter with the palette open never sends a prompt");
  assert.strictEqual(paletteEl.hidden, true, "accepting closes the palette");
  assert.strictEqual(paletteInput.value, "", "accepting clears the composer");
  assert.strictEqual(modelSelect.focused, true, "/model focuses the model picker");

  type(paletteInput, "/thi");
  assert.deepStrictEqual(paletteNames(paletteView), ["/thinking"], "filtering narrows to thinking");
  keydown(paletteInput, "Escape");
  assert.strictEqual(paletteEl.hidden, true, "Escape closes the palette");
  assert.deepStrictEqual(prompts, [], "closing the palette sends no prompt");

  type(paletteInput, "/thi");
  keydown(paletteInput, "ArrowDown");
  keydown(paletteInput, "Enter");
  assert.strictEqual(thinkingSelect.focused, true, "/thinking focuses the thinking picker");

  type(paletteInput, "/sys");
  keydown(paletteInput, "Tab");
  assert.strictEqual(systemPanel.hidden, false, "/system opens the system prompt panel");
  assert.strictEqual(systemInput.focused, true, "/system focuses the prompt textarea");
  type(paletteInput, "/sys");
  keydown(paletteInput, "Enter");
  assert.strictEqual(systemPanel.hidden, true, "/system toggles the panel closed");

  type(paletteInput, "/c");
  keydown(paletteInput, "Enter");
  await tick();
  assert.deepStrictEqual(
    paletteCalls.filter((command) => command.type === "compact"),
    [{ type: "compact" }],
    "/compact sends the canonical compact command"
  );
  assert.deepStrictEqual(prompts, [], "a slash command is never sent as a prompt");

  // Capability gating: an adapter with only model selection sees one entry,
  // and one with no slash-capable command sees no palette at all.
  const gated = window.WebtermAgent.create(
    { id: "p-2", title: "gated" },
    {
      onCommand: (command) =>
        Promise.resolve(
          command.type === "list_models" ? { models: [], default: "" } : {}
        ),
    }
  );
  gated.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: {
      prompt: true,
      compaction: false,
      models: { list: true, set: true, thinking: false },
      system_prompt: false,
    },
    dialogs: [],
    cursor: { seq: 0 },
  });
  type(findByClass(gated.element, "pi-input"), "/");
  assert.deepStrictEqual(paletteNames(gated), ["/model"], "capabilities hide the other entries");

  const bare = window.WebtermAgent.create(
    { id: "p-3", title: "bare" },
    { onCommand: () => Promise.resolve({}) }
  );
  bare.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  type(findByClass(bare.element, "pi-input"), "/");
  assert.strictEqual(paletteNames(bare), null, "no palette without slash capabilities");
  assert.strictEqual(findByClass(bare.element, "agent-palette").hidden, true, "palette stays hidden");

  // A draft with arguments is not a bare slash command, so it is a prompt and
  // the palette stays closed.
  type(paletteInput, "/model model-b");
  assert.strictEqual(paletteEl.hidden, true, "a draft with arguments does not open the palette");
  assert.ok(
    textOf(paletteView.element).includes("palette"),
    "the view still renders after the palette checks"
  );

  console.log("agent queue/palette smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
