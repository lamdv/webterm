/* Minimal DOM shim + smoke test for the canonical view's declared-command
 * layer: the palette merge, the generic arg form, the help dialog, and the
 * render-hint vocabulary (webterm/static/agent.js). */
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
    this._attributes = Object.create(null);
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.placeholder = "";
    this.rows = 0;
    this.type = "";
    this.title = "";
    this.href = "";
    this.download = "";
    this.rel = "";
    this.checked = false;
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

  setAttribute(name, value) {
    this._attributes[name] = value === undefined ? "" : String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attributes, name)
      ? this._attributes[name]
      : null;
  }

  focus() {
    this.focused = true;
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  dispatch(type, event) {
    const handler = this._listeners[type];
    if (handler) handler(event || { preventDefault() {} });
    if (type === "click" && this.tagName === "SUMMARY" && this.parent) {
      if (this.parent.tagName === "DETAILS") {
        this.parent.open = !this.parent.open;
        this.parent.dispatch("toggle");
      }
    }
  }

  querySelector(selector) {
    const found = this.querySelectorAll(selector);
    return found.length ? found[0] : null;
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

function findAllByClass(root, className) {
  const found = [];
  walk(root, (node) => {
    if (node && node._classes && node._classes.has(className)) found.push(node);
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

function paletteItem(view, name) {
  const palette = findByClass(view.element, "agent-palette");
  if (!palette || palette.hidden) return null;
  return (
    palette
      .querySelectorAll(".agent-palette-item")
      .find((item) => item.children[0].textContent === "/" + name) || null
  );
}

/** Open the palette on `/name` and accept it, like a human would. */
function runPaletteCommand(view, name) {
  const input = findByClass(view.element, "pi-input");
  type(input, "/" + name);
  keydown(input, "Enter");
  return input;
}

const declared = [
  {
    name: "export",
    title: "Export session",
    description: "Download HTML or JSONL",
    args: [
      {
        name: "format",
        type: "choice",
        title: "Format",
        description: "File format",
        required: true,
        choices: [
          { value: "html", label: "HTML" },
          { value: "jsonl", label: "JSONL" },
        ],
      },
    ],
  },
  { name: "limits", title: "Limits", description: "What cannot work here" },
  {
    name: "fork",
    title: "Fork",
    description: "Branch from an entry",
    danger: true,
    args: [
      { name: "entry_id", type: "string", title: "Entry", required: true },
      { name: "copy", type: "boolean", title: "Copy" },
    ],
  },
];

const help = [{ name: "/login", reason: "sign-in is a TUI-only screen" }];

function resync(extra) {
  return Object.assign(
    {
      type: "agent_resync",
      entries: [],
      state: { streaming: false, settled: true },
      capabilities: { prompt: true, abort: true, compaction: true },
      commands: declared,
      help,
      dialogs: [],
      cursor: { seq: 0 },
    },
    extra || {}
  );
}

(async () => {
  // ------------------------------------------------------------ palette merge
  // The core built-ins stay, and one entry per declared command follows them;
  // `/help` closes the list.  A declared command with `danger` is marked.
  const calls = [];
  const responses = {
    limits: { notice: { level: "warning", text: "no tree over this protocol" } },
    export: {
      download: {
        url: "/pi/download?path=%2Ftmp%2Fs.html",
        filename: "session.html",
        mime_type: "text/html",
      },
    },
    fork: { text: "forked from e1" },
  };
  const view = window.WebtermAgent.create(
    { id: "d-1", title: "declared" },
    {
      onCommand: (command) => {
        calls.push(command);
        if (command.type === "command" && command.name === "boom") {
          return Promise.reject(new Error("adapter refused: nope"));
        }
        if (command.type === "command" && Object.hasOwn(responses, command.name)) {
          return Promise.resolve(responses[command.name]);
        }
        return Promise.resolve({});
      },
    }
  );
  view.handle(resync());

  assert.deepStrictEqual(
    paletteNames(view),
    null,
    "the palette is closed before `/` is typed"
  );
  const input = findByClass(view.element, "pi-input");
  type(input, "/");
  assert.deepStrictEqual(
    paletteNames(view),
    ["/compact", "/export", "/limits", "/fork", "/help"],
    "declared commands are appended after the core built-ins, help last"
  );
  assert.deepStrictEqual(
    paletteItem(view, "limits").children.map((child) => child.textContent),
    ["/limits", "Limits", "What cannot work here"],
    "a declared entry shows its title and description"
  );
  assert.ok(
    paletteItem(view, "fork")._classes.has("danger"),
    "a dangerous command is marked in the palette"
  );
  assert.ok(
    findByClass(paletteItem(view, "fork"), "agent-palette-danger"),
    "the danger marker is visible"
  );
  assert.strictEqual(
    paletteItem(view, "export").title,
    "Export session",
    "the palette keeps the adapter's title as the tooltip"
  );
  assert.strictEqual(paletteItem(view, "compact").title, "", "built-ins carry no title");

  // ---------------------------------------------------------------- no args
  // A declared command without args is sent immediately, as the canonical
  // `command` record.
  runPaletteCommand(view, "limits");
  assert.deepStrictEqual(
    calls[calls.length - 1],
    { type: "command", name: "limits" },
    "a no-arg command is sent straight away"
  );
  await tick();
  const banner = findByClass(view.element, "agent-banner");
  assert.strictEqual(banner.hidden, false, "a notice hint shows the banner");
  assert.strictEqual(banner.textContent, "no tree over this protocol");
  assert.ok(banner._classes.has("warning"), "the notice keeps its level");

  // --------------------------------------------------------------- arg form
  // A command with args opens a form: text input, select for a choice, checkbox
  // for a boolean, and a local required check.
  runPaletteCommand(view, "export");
  const form = findByClass(view.element, "agent-command-form");
  assert.ok(form, "an arg'd command opens the form");
  assert.strictEqual(findByClass(view.element, "agent-modal-host").hidden, false);
  assert.strictEqual(findByClass(form, "agent-modal-title").textContent, "Export session");
  assert.strictEqual(findByClass(form, "agent-modal-desc").textContent, "Download HTML or JSONL");
  assert.strictEqual(
    calls.length,
    1,
    "opening the form sends nothing by itself"
  );
  const choice = findByClass(view.element, "agent-arg-choice");
  assert.deepStrictEqual(
    choice.children.map((option) => option.textContent),
    ["HTML", "JSONL"],
    "the select lists the declared choices"
  );
  assert.strictEqual(choice.value, "html", "a required choice starts on the first value");
  choice.value = "jsonl";
  findByClass(view.element, "agent-modal-submit").dispatch("click");
  assert.deepStrictEqual(
    calls[calls.length - 1],
    { type: "command", name: "export", args: { format: "jsonl" } },
    "the form sends the typed args"
  );
  assert.strictEqual(
    findByClass(view.element, "agent-command-form"),
    null,
    "submitting closes the form"
  );
  await tick();
  const downloadLink = findByClass(view.element, "agent-download-link");
  assert.ok(downloadLink, "a download hint renders a link");
  assert.strictEqual(downloadLink.href, "/pi/download?path=%2Ftmp%2Fs.html");
  assert.strictEqual(downloadLink.download, "session.html");
  assert.strictEqual(downloadLink.textContent, "session.html", "the label is the filename");
  assert.strictEqual(findByClass(view.element, "agent-download-mime").textContent, "text/html");

  // Required validation blocks the submit; nothing leaves the browser.
  runPaletteCommand(view, "fork");
  const beforeInvalid = calls.length;
  findByClass(view.element, "agent-modal-submit").dispatch("click");
  assert.strictEqual(calls.length, beforeInvalid, "an empty required arg blocks the submit");
  const error = findByClass(view.element, "agent-modal-error");
  assert.strictEqual(error.hidden, false);
  assert.strictEqual(error.textContent, "Required: Entry");
  assert.strictEqual(
    findByClass(view.element, "agent-modal-host").hidden,
    false,
    "the form stays open to be corrected"
  );

  const entryInput = findByClass(view.element, "agent-arg-string");
  entryInput.value = "e1";
  const copyBox = findByClass(view.element, "agent-arg-boolean");
  assert.strictEqual(copyBox.checked, false, "a boolean starts unchecked");
  copyBox.checked = true;
  findByClass(view.element, "agent-modal-submit").dispatch("click");
  assert.deepStrictEqual(
    calls[calls.length - 1],
    { type: "command", name: "fork", args: { entry_id: "e1", copy: true } },
    "text and boolean args are sent together"
  );
  await tick();
  const textResult = findByClass(view.element, "agent-result-text");
  assert.ok(textResult, "a text hint renders a <pre>");
  assert.strictEqual(textResult.textContent, "forked from e1");

  // Escape closes the form without sending anything.
  runPaletteCommand(view, "export");
  const beforeEscape = calls.length;
  keydown(findByClass(view.element, "agent-arg-choice"), "Escape");
  assert.strictEqual(
    findByClass(view.element, "agent-modal-host").hidden,
    true,
    "Escape closes the form"
  );
  assert.strictEqual(calls.length, beforeEscape, "closing the form sends nothing");
  await tick();

  // The server also answers `success:false` as a rejection: that must surface
  // through the notice path, not vanish.
  const failing = window.WebtermAgent.create(
    { id: "d-2", title: "failing" },
    {
      onCommand: () => Promise.reject(new Error("adapter refused: nope")),
    }
  );
  failing.handle(
    resync({ commands: [{ name: "boom", title: "Boom" }], help: [] })
  );
  runPaletteCommand(failing, "boom");
  await tick();
  const failureBanner = findByClass(failing.element, "agent-banner");
  assert.strictEqual(failureBanner.hidden, false, "a failed command shows the banner");
  assert.strictEqual(failureBanner.textContent, "adapter refused: nope");
  assert.ok(failureBanner._classes.has("error"), "the failure is an error notice");

  // ------------------------------------------------------------------- help
  // `/help` exists only while the adapter declares help entries, and lists
  // `name — reason`.
  const noHelp = window.WebtermAgent.create(
    { id: "d-3", title: "no help" },
    { onCommand: () => Promise.resolve({}) }
  );
  noHelp.handle(resync({ commands: [], help: [] }));
  type(findByClass(noHelp.element, "pi-input"), "/h");
  assert.strictEqual(paletteNames(noHelp), null, "no help entry without help entries");
  type(findByClass(noHelp.element, "pi-input"), "/");
  const bareNames = paletteNames(noHelp);
  assert.deepStrictEqual(
    bareNames,
    ["/compact"],
    "an adapter with no declared commands shows no palette entries beyond its capabilities"
  );

  type(input, "/hel");
  assert.deepStrictEqual(paletteNames(view), ["/help"], "the help entry filters like any other");
  keydown(input, "Enter");
  const helpDialog = findByClass(view.element, "agent-help");
  assert.ok(helpDialog, "the help dialog opens");
  const helpItem = findByClass(helpDialog, "agent-help-item");
  assert.strictEqual(findByClass(helpItem, "agent-help-name").textContent, "/login");
  assert.strictEqual(
    findText(helpItem, "agent-help-reason"),
    " — sign-in is a TUI-only screen",
    "the reason follows an em dash"
  );
  findByClass(helpDialog, "agent-modal-close").dispatch("click");
  assert.strictEqual(
    findByClass(view.element, "agent-modal-host").hidden,
    true,
    "the help dialog closes"
  );

  // ----------------------------------------------------------- render hints
  // Each hint in the vocabulary has one rendering: tree and JSON here, notice,
  // download, text and refresh above.
  const hintCalls = [];
  const hintResponses = {
    tree: {
      tree: {
        nodes: [
          {
            id: "root",
            label: "root",
            detail: "2 entries",
            children: [
              {
                id: "branch",
                label: "branch",
                actions: [
                  {
                    name: "fork",
                    label: "fork here",
                    command: { name: "fork", args: { entry_id: "e9" } },
                  },
                ],
              },
            ],
          },
        ],
        leaf_id: "branch",
      },
    },
    text: { text: "<b>not markup</b>" },
    refresh: { refresh: true },
    weird: { future: { shape: [1, 2] } },
    nolabel: { download: { url: "/download?path=x" } },
  };
  const hints = window.WebtermAgent.create(
    { id: "d-4", title: "hints" },
    {
      onCommand: (command) => {
        hintCalls.push(command);
        if (command.type === "command" && Object.hasOwn(hintResponses, command.name)) {
          return Promise.resolve(hintResponses[command.name]);
        }
        return Promise.resolve({});
      },
    }
  );
  hints.handle(
    resync({
      commands: [
        { name: "tree", title: "Session tree" },
        { name: "text", title: "Raw output" },
        { name: "refresh", title: "Switch session" },
        { name: "weird", title: "Future thing" },
        { name: "nolabel", title: "Anonymous download" },
      ],
      help: [],
    })
  );

  runPaletteCommand(hints, "tree");
  await tick();
  const treeNode = findByClass(hints.element, "agent-tree-node");
  assert.ok(treeNode, "a tree hint renders nodes");
  assert.strictEqual(
    findByClass(treeNode, "agent-tree-label").textContent,
    "root",
    "the node label comes from the hint"
  );
  assert.strictEqual(findByClass(treeNode, "agent-tree-detail").textContent, "2 entries");
  assert.strictEqual(treeNode.open, true, "a top-level branch starts open");
  const child = findByClass(treeNode, "agent-tree-node branch") ||
    findAllByClass(hints.element, "agent-tree-node")[1];
  assert.ok(child, "nested children render");
  assert.ok(
    findByClass(child, "agent-tree-label").textContent === "branch",
    "the child label renders"
  );
  assert.strictEqual(
    findByClass(child, "agent-tree-leaf").textContent,
    "current",
    "the leaf node is marked"
  );
  const actionButton = findByClass(child, "agent-tree-action");
  assert.strictEqual(actionButton.textContent, "fork here");
  const beforeAction = hintCalls.length;
  actionButton.dispatch("click");
  assert.deepStrictEqual(
    hintCalls[beforeAction],
    { type: "command", name: "fork", args: { entry_id: "e9" } },
    "a node action sends its declared command"
  );
  await tick();

  runPaletteCommand(hints, "text");
  await tick();
  const pre = findByClass(hints.element, "agent-result-text");
  assert.strictEqual(
    pre.textContent,
    "<b>not markup</b>",
    "a text hint is rendered as text, never as markup"
  );

  runPaletteCommand(hints, "refresh");
  await tick();
  assert.deepStrictEqual(
    hintCalls[hintCalls.length - 1],
    { type: "get_snapshot" },
    "a refresh hint re-requests a snapshot"
  );

  runPaletteCommand(hints, "weird");
  await tick();
  const jsonCard = findByClass(hints.element, "agent-result-json");
  assert.ok(jsonCard, "unknown data renders a collapsible JSON card");
  assert.strictEqual(jsonCard.tagName, "DETAILS");
  assert.ok(
    textOf(jsonCard).includes('"future"'),
    "the JSON card shows the adapter's data"
  );
  assert.ok(
    textOf(jsonCard).includes("1"),
    "the JSON card is not truncated"
  );

  runPaletteCommand(hints, "nolabel");
  await tick();
  const lastLink = findAllByClass(hints.element, "agent-download-link").pop();
  assert.strictEqual(lastLink.textContent, "download", "a download without a filename says so");
  assert.strictEqual(lastLink.href, "/download?path=x");
  findByClass(findByClass(hints.element, "agent-download"), "agent-modal-close").dispatch("click");

  // A resync replaces the manifest, so an open form (built from the old one)
  // must not survive it.
  runPaletteCommand(view, "export");
  assert.strictEqual(
    findByClass(view.element, "agent-modal-host").hidden,
    false,
    "the form is open before the resync"
  );
  view.handle(resync({ commands: [], help: [] }));
  assert.strictEqual(
    findByClass(view.element, "agent-modal-host").hidden,
    true,
    "a resync closes the form"
  );
  type(findByClass(view.element, "pi-input"), "/");
  assert.deepStrictEqual(
    paletteNames(view),
    ["/compact"],
    "a resync without commands drops the declared entries"
  );
  view.setExited();

  console.log("agent declared-command smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

/** First node with `className` whose parent chain leads to `root`; the tree
 * test needs the child reason span, not just any match. */
function findText(root, className) {
  const node = findByClass(root, className);
  return node ? node.textContent : "";
}
