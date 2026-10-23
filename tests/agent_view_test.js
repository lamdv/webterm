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
  focus() {}

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  dispatch(type, event) {
    const handler = this._listeners[type];
    if (handler) handler(event || { preventDefault() {} });
    // Browsers toggle a <details> when its <summary> is clicked and then fire
    // `toggle`; model that so the open/closed state is observable here.
    if (type === "click" && this.tagName === "SUMMARY" && this.parent) {
      if (this.parent.tagName === "DETAILS") {
        this.parent.open = !this.parent.open;
        this.parent.dispatch("toggle");
      }
    }
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

// The thinking *picker* shares the agent-thinking class, so the transcript's
// <details> block is found from the message list.
function findThinking(root) {
  const messages = findByClass(root, "agent-messages") || root;
  let found = null;
  walk(messages, (node) => {
    if (!found && node && node._classes && node._classes.has("agent-thinking")) {
      if (node.tagName === "DETAILS") found = node;
    }
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

// Model, thinking and system-prompt controls: each selection becomes one
// canonical command, and the view tracks the value it last set.
const controlCalls = [];
const controlled = window.WebtermAgent.create(
  { id: "3-1", title: "controls", model: "model-a" },
  {
    onCommand: (command) => {
      controlCalls.push(command);
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
controlled.handle({
  type: "agent_resync",
  entries: [],
  state: {
    model: "model-a",
    streaming: false,
    settled: true,
    thinking_level: "default",
    system_prompt: "Be terse.",
  },
  capabilities: {
    prompt: true,
    abort: true,
    models: {
      list: true,
      set: true,
      thinking: true,
      levels: ["default", "low", "high"],
    },
    system_prompt: true,
  },
  dialogs: [],
  cursor: { seq: 1 },
});

const controls = findByClass(controlled.element, "agent-controls");
assert.ok(controls && controls.hidden === false, "control bar rendered");
const modelSelect = findByClass(controlled.element, "agent-model");
assert.ok(modelSelect && !modelSelect.hidden, "model picker rendered");
const thinkingSelect = findByClass(controlled.element, "agent-thinking");
assert.ok(thinkingSelect && !thinkingSelect.hidden, "thinking picker rendered");
assert.strictEqual(
  findByClass(controlled.element, "agent-system-toggle").textContent,
  "system prompt · set",
  "system prompt button shows that a prompt is set"
);

(async () => {
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(textOf(paged.element).includes("older message"), "earlier page prepended");
  assert.strictEqual(
    findByClass(paged.element, "pi-history-more"),
    null,
    "load-earlier button gone once the history is exhausted"
  );

  assert.deepStrictEqual(
    modelSelect.children.map((option) => option.value),
    ["model-a", "model-b"],
    "model picker populated from list_models"
  );
  assert.strictEqual(modelSelect.value, "model-a", "current model selected");
  assert.deepStrictEqual(
    thinkingSelect.children.map((option) => option.value),
    ["default", "low", "high"],
    "thinking picker uses the adapter's levels"
  );
  assert.strictEqual(thinkingSelect.value, "default", "current thinking level selected");

  thinkingSelect.value = "high";
  thinkingSelect.dispatch("change");
  assert.deepStrictEqual(
    controlCalls[controlCalls.length - 1],
    { type: "set_thinking", level: "high" },
    "thinking selection sends set_thinking"
  );

  modelSelect.value = "model-b";
  modelSelect.dispatch("change");
  assert.deepStrictEqual(
    controlCalls[controlCalls.length - 1],
    { type: "set_model", model: "model-b" },
    "model selection sends set_model"
  );

  const systemToggle = findByClass(controlled.element, "agent-system-toggle");
  systemToggle.dispatch("click");
  const systemPanel = findByClass(controlled.element, "agent-system-panel");
  assert.ok(!systemPanel.hidden, "system prompt panel opens");
  const systemInput = findByClass(controlled.element, "agent-system-input");
  assert.strictEqual(systemInput.value, "Be terse.", "current system prompt prefilled");
  systemInput.value = "Be brief.";
  findByClass(controlled.element, "agent-system-save").dispatch("click");
  assert.deepStrictEqual(
    controlCalls[controlCalls.length - 1],
    { type: "set_system_prompt", text: "Be brief." },
    "saving the panel sends set_system_prompt"
  );

  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(systemPanel.hidden, "system prompt panel closes after saving");
  assert.ok(
    textOf(controlled.element).includes("thinking high"),
    "status bar reports the selected thinking level"
  );

  // Streaming deltas must patch the live message in place: a thinking block
  // the reader opened during a run has to stay open (and keep its streamed
  // text) across the deltas, the end of the run, and an older history page.
  const streaming = window.WebtermAgent.create(
    { id: "4-1", title: "streaming" },
    {
      onHistory: () =>
        Promise.resolve({
          entries: [
            { id: "old-1", role: "user", content: [{ type: "text", text: "earlier question" }] },
          ],
          has_more: false,
          first_entry_id: "old-1",
        }),
    }
  );
  streaming.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, abort: true, history_paging: true },
    has_more: true,
    first_entry_id: "think-1",
    dialogs: [],
    cursor: { seq: 0 },
  });
  streaming.handle({
    type: "agent_event",
    event: {
      event: "message.started",
      message: { id: "think-1", role: "assistant", content: [] },
    },
  });
  streaming.handle({
    type: "agent_event",
    event: {
      event: "message.delta",
      message_id: "think-1",
      block_index: 0,
      block_type: "thinking",
      delta: "weighing",
    },
  });

  const thinking = findThinking(streaming.element);
  assert.ok(thinking, "thinking block rendered");
  thinking.querySelectorAll("summary")[0].dispatch("click");
  assert.strictEqual(thinking.open, true, "clicking Thinking opens the block");

  streaming.handle({
    type: "agent_event",
    event: {
      event: "message.delta",
      message_id: "think-1",
      block_index: 0,
      block_type: "thinking",
      delta: " more",
    },
  });
  const streamed = findThinking(streaming.element);
  assert.strictEqual(streamed.open, true, "thinking stays open while tokens stream");
  assert.ok(
    textOf(streamed).includes("weighing more"),
    "streamed thinking text is complete"
  );
  assert.ok(
    streamed === thinking,
    "live thinking block keeps its DOM node across deltas"
  );

  // A new block changes the entry structure; the rebuild must carry the
  // open state over instead of dropping it.
  streaming.handle({
    type: "agent_event",
    event: {
      event: "message.delta",
      message_id: "think-1",
      block_index: 1,
      block_type: "text",
      delta: "answer so far",
    },
  });
  const afterBlock = findThinking(streaming.element);
  assert.ok(afterBlock, "thinking block still rendered after a new block");
  assert.strictEqual(afterBlock.open, true, "thinking stays open when a block is added");
  assert.ok(
    textOf(afterBlock).includes("weighing more"),
    "thinking text survives a structural rebuild"
  );

  streaming.handle({
    type: "agent_event",
    event: {
      event: "message.finished",
      message: {
        id: "think-1",
        role: "assistant",
        content: [
          { type: "thinking", text: "weighing more carefully" },
          { type: "text", text: "answer so far" },
        ],
      },
    },
  });
  const finished = findThinking(streaming.element);
  assert.strictEqual(finished.open, true, "thinking stays open after the run finishes");
  assert.ok(
    textOf(finished).includes("weighing more carefully"),
    "finished message text is complete"
  );

  findByClass(streaming.element, "pi-history-more").dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(
    textOf(streaming.element).includes("earlier question"),
    "older history prepended after streaming"
  );
  assert.strictEqual(
    findThinking(streaming.element).open,
    true,
    "thinking stays open after older history is prepended"
  );
  assert.ok(
    textOf(streaming.element).indexOf("earlier question") <
      textOf(streaming.element).indexOf("answer so far"),
    "older history is ordered before the newer entry"
  );

  // ---------------------------------------------------------------- library
  // The saved-prompt picker: listed lazily on first panel open, fill-only on
  // selection, and save/delete through the canonical commands.  Every change
  // re-reads the list, and the adapter's reply (not the typed name) wins.
  const libraryCalls = [];
  const libraryPrompts = [
    { name: "code-review", text: "Review the diff." },
    { name: "concise", text: "Be terse." },
  ];
  const library = window.WebtermAgent.create(
    { id: "5-1", title: "library" },
    {
      onCommand: (command) => {
        libraryCalls.push(command);
        if (command.type === "list_prompts") {
          return Promise.resolve({ prompts: libraryPrompts.slice() });
        }
        if (command.type === "save_prompt") {
          const saved = { name: command.name.trim().toLowerCase(), text: command.text.trim() };
          const at = libraryPrompts.findIndex((prompt) => prompt.name === saved.name);
          if (at >= 0) libraryPrompts[at] = saved;
          else libraryPrompts.push(saved);
          libraryPrompts.sort((a, b) => a.name.localeCompare(b.name));
          return Promise.resolve({ prompt: saved });
        }
        if (command.type === "delete_prompt") {
          const at = libraryPrompts.findIndex((prompt) => prompt.name === command.name);
          if (at >= 0) libraryPrompts.splice(at, 1);
          return Promise.resolve({ name: command.name, deleted: at >= 0 });
        }
        return Promise.resolve({});
      },
    }
  );
  library.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true, system_prompt: "Be terse." },
    capabilities: { prompt: true, abort: true, system_prompt: true, prompt_library: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  const libraryRow = findByClass(library.element, "agent-system-library");
  assert.ok(libraryRow, "library row exists when the capability is advertised");
  assert.strictEqual(libraryRow.hidden, false, "library row is shown with the capability");
  assert.deepStrictEqual(libraryCalls, [], "no prompt command is sent before the panel opens");

  findByClass(library.element, "agent-system-toggle").dispatch("click");
  await tick();
  assert.deepStrictEqual(
    libraryCalls,
    [{ type: "list_prompts" }],
    "list_prompts is requested on the first panel open"
  );
  const promptPick = findByClass(library.element, "agent-prompt-pick");
  assert.ok(promptPick, "prompt picker rendered");
  assert.deepStrictEqual(
    promptPick.children.map((option) => option.value),
    ["", "code-review", "concise"],
    "placeholder first, then the saved prompts sorted by name"
  );
  assert.strictEqual(
    promptPick.children[0].textContent,
    "saved prompts…",
    "first option is the placeholder"
  );
  assert.strictEqual(promptPick.value, "", "nothing selected initially");

  const libraryInput = findByClass(library.element, "agent-system-input");
  promptPick.value = "code-review";
  promptPick.dispatch("change");
  assert.strictEqual(
    libraryInput.value,
    "Review the diff.",
    "picking a prompt fills the textarea"
  );
  assert.deepStrictEqual(libraryCalls, [{ type: "list_prompts" }], "picking sends no command");

  findByClass(library.element, "agent-system-library-save").dispatch("click");
  const libraryForm = findByClass(library.element, "agent-system-library-form");
  const libraryName = findByClass(library.element, "agent-system-library-name");
  assert.strictEqual(libraryForm.hidden, false, "Save to library reveals the name field");
  assert.strictEqual(
    libraryName.value,
    "code-review",
    "name field is prefilled with the selected prompt"
  );
  libraryName.value = "Deep Review";
  libraryInput.value = "Be thorough.";
  findByClass(library.element, "agent-system-library-commit").dispatch("click");
  assert.deepStrictEqual(
    libraryCalls[libraryCalls.length - 1],
    { type: "save_prompt", name: "Deep Review", text: "Be thorough." },
    "saving sends save_prompt with the typed name and the textarea text"
  );
  assert.deepStrictEqual(
    libraryCalls.filter((call) => call.type === "set_system_prompt"),
    [],
    "saving a prompt does not apply it"
  );
  await tick();
  assert.strictEqual(
    libraryCalls.filter((call) => call.type === "list_prompts").length,
    2,
    "the list is refreshed after saving"
  );
  assert.strictEqual(promptPick.value, "deep review", "the saved name is selected");
  assert.ok(
    textOf(library.element).includes("saved prompt: deep review"),
    "the notice names the saved (normalized) prompt"
  );
  assert.strictEqual(libraryForm.hidden, true, "the name field closes after saving");

  promptPick.value = "concise";
  promptPick.dispatch("change");
  const deleteButton = findByClass(library.element, "agent-system-library-delete");
  const deletes = () => libraryCalls.filter((call) => call.type === "delete_prompt");
  assert.strictEqual(deleteButton.disabled, false, "delete is enabled for a saved prompt");
  deleteButton.dispatch("click");
  assert.deepStrictEqual(deletes(), [], "the first click only arms the delete");
  assert.strictEqual(deleteButton.textContent, "Delete concise?", "the armed delete names it");
  promptPick.value = "code-review";
  promptPick.dispatch("change");
  assert.strictEqual(deleteButton.textContent, "Delete", "selection change disarms the delete");
  promptPick.value = "concise";
  promptPick.dispatch("change");
  deleteButton.dispatch("click");
  deleteButton.dispatch("click");
  assert.deepStrictEqual(
    deletes(),
    [{ type: "delete_prompt", name: "concise" }],
    "the second click sends delete_prompt once"
  );
  await tick();
  assert.ok(
    textOf(library.element).includes("deleted prompt: concise"),
    "the deletion is reported"
  );
  assert.strictEqual(promptPick.value, "", "the selection is cleared after deleting");
  assert.deepStrictEqual(
    promptPick.children.map((option) => option.value),
    ["", "code-review", "deep review"],
    "the list is refreshed after deleting"
  );
  assert.strictEqual(deleteButton.disabled, true, "delete is disabled without a selection");

  promptPick.value = "code-review";
  promptPick.dispatch("change");
  deleteButton.dispatch("click");
  assert.strictEqual(deleteButton.textContent, "Delete code-review?", "delete re-arms");
  findByClass(library.element, "agent-system-close").dispatch("click");
  assert.strictEqual(deleteButton.textContent, "Delete", "closing the panel disarms the delete");
  assert.ok(libraryForm.hidden, "closing the panel hides the name field");

  // A rejected save surfaces the server's error and keeps the name field open.
  const errorCalls = [];
  const rejecting = window.WebtermAgent.create(
    { id: "6-1", title: "library errors" },
    {
      onCommand: (command) => {
        errorCalls.push(command);
        if (command.type === "list_prompts") {
          return Promise.resolve({ prompts: [{ name: "concise", text: "Be terse." }] });
        }
        if (command.type === "save_prompt") {
          return Promise.reject(new Error("invalid prompt name"));
        }
        return Promise.resolve({});
      },
    }
  );
  rejecting.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true, system_prompt: "Stay short." },
    capabilities: { prompt: true, system_prompt: true, prompt_library: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  findByClass(rejecting.element, "agent-system-toggle").dispatch("click");
  await tick();
  findByClass(rejecting.element, "agent-system-library-save").dispatch("click");
  const rejectingName = findByClass(rejecting.element, "agent-system-library-name");
  rejectingName.value = "bad/name";
  findByClass(rejecting.element, "agent-system-library-commit").dispatch("click");
  await tick();
  assert.ok(
    textOf(rejecting.element).includes("invalid prompt name"),
    "a rejected save shows the server error"
  );
  assert.strictEqual(
    findByClass(rejecting.element, "agent-system-library-form").hidden,
    false,
    "the name field stays open after an error"
  );
  assert.strictEqual(rejectingName.value, "bad/name", "the rejected name is kept for editing");
  assert.deepStrictEqual(
    errorCalls.filter((call) => call.type === "save_prompt"),
    [{ type: "save_prompt", name: "bad/name", text: "Stay short." }],
    "the failing save was attempted once"
  );

  // Without the capability the row is hidden and no prompt command is sent.
  const plainCalls = [];
  const plain = window.WebtermAgent.create(
    { id: "7-1", title: "no library" },
    {
      onCommand: (command) => {
        plainCalls.push(command);
        return Promise.resolve({});
      },
    }
  );
  plain.handle({
    type: "agent_resync",
    entries: [],
    state: { streaming: false, settled: true, system_prompt: "Keep it short." },
    capabilities: { prompt: true, system_prompt: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  assert.strictEqual(
    findByClass(plain.element, "agent-system-library").hidden,
    true,
    "the library row is hidden without the capability"
  );
  findByClass(plain.element, "agent-system-toggle").dispatch("click");
  await tick();
  assert.deepStrictEqual(plainCalls, [], "no prompt command without the capability");
  const plainPanel = findByClass(plain.element, "agent-system-panel");
  assert.strictEqual(plainPanel.hidden, false, "the system prompt panel still opens");
  assert.strictEqual(
    findByClass(plain.element, "agent-system-input").value,
    "Keep it short.",
    "the prompt textarea is unchanged"
  );
  for (const name of [
    "agent-system-save",
    "agent-system-clear",
    "agent-system-close",
  ]) {
    assert.ok(findByClass(plain.element, name), name + " still rendered");
  }

  // ---------------------------------------------------------------- editing
  // Editing a past user message: capability-gated edit button, an in-place
  // textarea, one edit_message command, and a transcript truncation driven by
  // the adapter's transcript.truncated event rather than by the response.
  const editCalls = [];
  const editing = window.WebtermAgent.create(
    { id: "8-1", title: "editing" },
    {
      onCommand: (command) => {
        editCalls.push(command);
        if (command.type === "edit_message") {
          return Promise.resolve({
            disposition: "started",
            entry_id: "e5",
            removed: 3,
          });
        }
        return Promise.resolve({});
      },
    }
  );
  editing.handle({
    type: "agent_resync",
    entries: [
      { id: "e1", role: "user", content: [{ type: "text", text: "first question" }] },
      { id: "e2", role: "assistant", content: [{ type: "text", text: "first answer" }] },
      { id: "e3", role: "user", content: [{ type: "text", text: "second question" }] },
      { id: "e4", role: "assistant", content: [{ type: "text", text: "second answer" }] },
    ],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, abort: true, edit_message: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  const editButtons = () => editing.element.querySelectorAll(".agent-edit");
  assert.strictEqual(
    editButtons().length,
    2,
    "only user messages carry an edit button"
  );

  // Cancel sends nothing and restores the message text.
  editButtons()[0].dispatch("click");
  let editField = findByClass(editing.element, "agent-edit-input");
  assert.ok(editField, "clicking edit swaps the message for a textarea");
  assert.strictEqual(editField.value, "first question", "the textarea starts with the text");
  assert.strictEqual(
    editButtons().length,
    1,
    "the entry being edited loses its edit button"
  );
  findByClass(editing.element, "agent-edit-cancel").dispatch("click");
  assert.strictEqual(findByClass(editing.element, "agent-edit-input"), null, "cancel closes the editor");
  assert.ok(textOf(editing.element).includes("first question"), "the message is unchanged");
  assert.deepStrictEqual(editCalls, [], "cancel sends no command");

  // An empty draft is rejected locally.
  editButtons()[0].dispatch("click");
  editField = findByClass(editing.element, "agent-edit-input");
  editField.value = "   ";
  editField.dispatch("input");
  findByClass(editing.element, "agent-edit-save").dispatch("click");
  assert.deepStrictEqual(editCalls, [], "an empty edit is not sent");
  assert.ok(
    textOf(editing.element).includes("cannot be empty"),
    "the empty draft is reported"
  );

  // Save sends one edit_message and reports how much was dropped.
  editField.value = "rewritten question";
  editField.dispatch("input");
  findByClass(editing.element, "agent-edit-save").dispatch("click");
  assert.deepStrictEqual(
    editCalls,
    [{ type: "edit_message", entry_id: "e1", message: "rewritten question" }],
    "save sends the edited text for that entry"
  );
  await tick();
  assert.ok(
    textOf(editing.element).includes("message edited; 2 later message(s) dropped"),
    "the outcome names the dropped messages"
  );

  // The adapter owns the truncation: the transcript follows its event.
  editing.handle({
    type: "agent_event",
    event: { event: "transcript.truncated", entry_id: "e1", removed: 3 },
  });
  editing.handle({
    type: "agent_event",
    event: {
      event: "message.finished",
      message: {
        id: "e5",
        role: "user",
        content: [{ type: "text", text: "rewritten question" }],
      },
    },
  });
  const transcript = textOf(editing.element);
  assert.ok(transcript.includes("rewritten question"), "the edited message is shown");
  for (const gone of ["first answer", "second question", "second answer"]) {
    assert.ok(!transcript.includes(gone), gone + " was dropped from the transcript");
  }
  assert.strictEqual(
    editing.element.querySelectorAll(".agent-msg").length,
    1,
    "only the edited message remains"
  );
  assert.strictEqual(editButtons().length, 1, "the re-sent message is editable again");

  // A run in flight refuses the edit before it reaches the adapter.
  editing.handle({
    type: "agent_event",
    event: { event: "run.started", run_id: "run-9" },
  });
  editButtons()[0].dispatch("click");
  editField = findByClass(editing.element, "agent-edit-input");
  editField.value = "too soon";
  editField.dispatch("input");
  findByClass(editing.element, "agent-edit-save").dispatch("click");
  assert.strictEqual(
    editCalls.length,
    1,
    "a busy session does not send another edit_message"
  );
  assert.ok(
    textOf(editing.element).includes("Wait for the current reply"),
    "the busy refusal is shown"
  );
  editing.handle({
    type: "agent_event",
    event: { event: "run.finished", run_id: "run-9", reason: "stop" },
  });
  editing.handle({
    type: "agent_event",
    event: { event: "run.settled", run_id: "run-9" },
  });

  // ------------------------------------------------ editing an agent answer
  // An answer is editable only when the adapter advertises `edit_answer`; the
  // same command rewrites the transcript and starts no run, so the button says
  // just Save.
  const answerCalls = [];
  const answering = window.WebtermAgent.create(
    { id: "10-1", title: "answering" },
    {
      onCommand: (command) => {
        answerCalls.push(command);
        if (command.type === "edit_message") {
          return Promise.resolve({ disposition: "updated", entry_id: "a5", removed: 3 });
        }
        return Promise.resolve({});
      },
    }
  );
  answering.handle({
    type: "agent_resync",
    entries: [
      { id: "a1", role: "user", content: [{ type: "text", text: "first question" }] },
      { id: "a2", role: "assistant", content: [{ type: "text", text: "first answer" }] },
      { id: "a3", role: "user", content: [{ type: "text", text: "second question" }] },
      { id: "a4", role: "assistant", content: [{ type: "text", text: "second answer" }] },
    ],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, abort: true, edit_answer: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  const answerButtons = () => answering.element.querySelectorAll(".agent-edit");
  assert.strictEqual(
    answerButtons().length,
    2,
    "only assistant answers carry an edit button for edit_answer"
  );

  answerButtons()[0].dispatch("click");
  const answerField = findByClass(answering.element, "agent-edit-input");
  assert.ok(answerField, "clicking edit swaps the answer for a textarea");
  assert.strictEqual(answerField.value, "first answer", "the textarea starts with the answer");
  const answerSave = findByClass(answering.element, "agent-edit-save");
  assert.strictEqual(answerSave.textContent, "Save", "an answer is saved, not resent");

  answerField.value = "corrected answer";
  answerField.dispatch("input");
  answerSave.dispatch("click");
  assert.deepStrictEqual(
    answerCalls,
    [{ type: "edit_message", entry_id: "a2", message: "corrected answer" }],
    "saving an answer sends one edit_message"
  );
  await tick();
  assert.ok(
    textOf(answering.element).includes("answer edited; 2 later message(s) dropped"),
    "the answer outcome names the dropped messages"
  );

  answering.handle({
    type: "agent_event",
    event: { event: "transcript.truncated", entry_id: "a2", removed: 3 },
  });
  answering.handle({
    type: "agent_event",
    event: {
      event: "message.finished",
      message: {
        id: "a5",
        role: "assistant",
        content: [{ type: "text", text: "corrected answer" }],
      },
    },
  });
  const answerTranscript = textOf(answering.element);
  assert.ok(answerTranscript.includes("corrected answer"), "the corrected answer is shown");
  for (const gone of ["first answer", "second question", "second answer"]) {
    assert.ok(!answerTranscript.includes(gone), gone + " was dropped from the transcript");
  }
  assert.strictEqual(
    answering.element.querySelectorAll(".agent-msg").length,
    2,
    "the question and the corrected answer remain"
  );
  assert.strictEqual(
    answerCalls.length,
    1,
    "an answer edit sends nothing besides the edit_message"
  );

  // Without the capability no edit affordance exists at all.
  const noEdit = window.WebtermAgent.create(
    { id: "9-1", title: "no editing" },
    { onCommand: () => Promise.resolve({}) }
  );
  noEdit.handle({
    type: "agent_resync",
    entries: [
      { id: "e1", role: "user", content: [{ type: "text", text: "hello" }] },
      { id: "e2", role: "assistant", content: [{ type: "text", text: "hi" }] },
    ],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  assert.strictEqual(
    noEdit.element.querySelectorAll(".agent-edit").length,
    0,
    "no edit button without the capability"
  );

  // edit_message alone does not make answers editable.
  noEdit.handle({
    type: "agent_resync",
    entries: [
      { id: "e1", role: "user", content: [{ type: "text", text: "hello" }] },
      { id: "e2", role: "assistant", content: [{ type: "text", text: "hi" }] },
    ],
    state: { streaming: false, settled: true },
    capabilities: { prompt: true, edit_message: true },
    dialogs: [],
    cursor: { seq: 0 },
  });
  assert.strictEqual(
    noEdit.element.querySelectorAll(".agent-edit").length,
    1,
    "edit_message covers user messages only"
  );

  console.log("agent view smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
