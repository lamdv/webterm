/* Minimal DOM shim + smoke test for webterm/static/attach.js.
 *
 * Covers the composer image path the views depend on: paste, drag/drop, the
 * thumbnail strip and its per-image remove, capability gating, the local size
 * checks, and the canonical `{mime_type, data}` shape sent to the server.
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
    this._listeners = Object.create(null);
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.title = "";
    this.alt = "";
    this.src = "";
  }

  get className() {
    return [...this._classes].join(" ");
  }

  set className(value) {
    this._classes = new Set(String(value || "").split(/\s+/).filter(Boolean));
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

  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
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
}

global.document = { createElement: (tag) => new FakeNode(tag) };
global.window = global;

require(path.join(__dirname, "..", "webterm", "static", "attach.js"));

const Attach = window.WebtermAttach;
assert.ok(Attach, "WebtermAttach is exported");

// The browser's limits are advisory copies of the server's
// (webterm/agent/protocol.py); they must not drift apart.
assert.strictEqual(Attach.MAX_IMAGES, 4, "four images per message");
assert.strictEqual(Attach.MAX_IMAGE_BYTES, 3 * 1024 * 1024, "3 MiB per image");
assert.strictEqual(Attach.MAX_TOTAL_BYTES, 4 * 1024 * 1024, "4 MiB per message");
assert.deepStrictEqual(
  Attach.MIME_TYPES.slice().sort(),
  ["image/gif", "image/jpeg", "image/png", "image/webp"],
  "supported image types"
);

const PNG_HEADER = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function bytesOf(size) {
  const bytes = new Uint8Array(size);
  PNG_HEADER.forEach((value, index) => {
    bytes[index] = value;
  });
  return bytes;
}

function imageFile(type, size, name) {
  const bytes = bytesOf(size);
  return {
    name: name || "image",
    type,
    size,
    arrayBuffer: () => Promise.resolve(bytes.buffer.slice(0)),
  };
}

function pasteEvent(files) {
  const state = { prevented: false };
  state.event = {
    clipboardData: { files },
    preventDefault: () => {
      state.prevented = true;
    },
  };
  return state;
}

function dropEvent(files) {
  const state = { prevented: false };
  state.event = {
    dataTransfer: { types: ["Files"], files },
    preventDefault: () => {
      state.prevented = true;
    },
  };
  return state;
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

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

async function main() {
  const notices = [];
  let changes = 0;
  const form = new FakeNode("form");
  const input = new FakeNode("textarea");
  const controller = Attach.create({
    form,
    input,
    onNotice: (text) => notices.push(text),
    onChange: () => {
      changes += 1;
    },
  });

  assert.strictEqual(controller.element.className, "pi-attach-strip");
  assert.strictEqual(controller.element.hidden, true, "empty strip is hidden");
  assert.strictEqual(controller.isEnabled(), false, "views must enable it explicitly");

  // Gated off: a paste is refused with a clear notice, nothing is stored.
  let paste = pasteEvent([imageFile("image/png", 128)]);
  input.dispatch("paste", paste.event);
  await tick();
  assert.strictEqual(paste.prevented, true, "an image paste is intercepted");
  assert.strictEqual(controller.count(), 0, "no image stored while disabled");
  assert.deepStrictEqual(notices, ["this agent does not accept images"]);

  controller.setEnabled(true);
  notices.length = 0;

  // Paste of one image becomes a canonical entry with raw base64.
  const expected = Buffer.from(bytesOf(128)).toString("base64");
  paste = pasteEvent([imageFile("image/png", 128)]);
  input.dispatch("paste", paste.event);
  assert.strictEqual(paste.prevented, true, "an accepted paste is intercepted");
  await tick();
  let images = controller.images();
  assert.strictEqual(images.length, 1);
  assert.deepStrictEqual(Object.keys(images[0]).sort(), ["data", "mime_type"]);
  assert.strictEqual(images[0].mime_type, "image/png");
  assert.strictEqual(images[0].data, expected);
  assert.ok(images[0].data.startsWith("iVBOR"), "raw base64, no data URL prefix");
  assert.strictEqual(controller.element.hidden, false, "strip shows pending images");

  const thumbs = byClass(controller.element, "pi-attach-thumb");
  const removes = byClass(controller.element, "pi-attach-remove");
  assert.strictEqual(thumbs.length, 1, "one thumbnail per image");
  assert.ok(thumbs[0].src.startsWith("data:image/png;base64,"));
  assert.strictEqual(removes.length, 1, "one remove control per image");

  // Per-image remove empties the strip again.
  const before = changes;
  removes[0].dispatch("click", { preventDefault() {} });
  assert.strictEqual(controller.count(), 0, "remove dropped the image");
  assert.strictEqual(controller.element.hidden, true, "strip hides when empty");
  assert.ok(changes > before, "removal notifies the view");

  // Drag/drop onto the composer adds an image, and a non-image drop is left
  // alone so the browser keeps its own behaviour.
  let drag = dropEvent([imageFile("image/jpeg", 256, "photo.jpg")]);
  form.dispatch("dragover", drag.event);
  assert.strictEqual(drag.prevented, true, "dropping files is allowed");
  form.dispatch("drop", drag.event);
  assert.strictEqual(drag.prevented, true, "the drop is handled here");
  await tick();
  images = controller.images();
  assert.strictEqual(images.length, 1);
  assert.strictEqual(images[0].mime_type, "image/jpeg");

  drag = dropEvent([{ name: "notes.txt", type: "text/plain", size: 10 }]);
  form.dispatch("drop", drag.event);
  await tick();
  assert.strictEqual(drag.prevented, false, "a non-image drop is not swallowed");
  assert.strictEqual(controller.count(), 1);

  // Local pre-checks mirror the server's distinct error texts.
  controller.clear();
  notices.length = 0;
  await controller.addFiles([imageFile("image/tiff", 64)]);
  assert.deepStrictEqual(notices, ["unsupported image type: image/tiff"]);

  notices.length = 0;
  await controller.addFiles([imageFile("image/png", Attach.MAX_IMAGE_BYTES + 1)]);
  assert.deepStrictEqual(notices, ["image too large: at most 3 MiB each"]);

  notices.length = 0;
  const many = [];
  for (let index = 0; index < Attach.MAX_IMAGES + 1; index += 1) {
    many.push(imageFile("image/png", 64));
  }
  await controller.addFiles(many);
  assert.deepStrictEqual(notices, ["too many images: at most 4 per message"]);
  assert.strictEqual(controller.count(), 0);

  // Per-message total: four images that each fit still exceed the total.
  notices.length = 0;
  const big = [];
  for (let index = 0; index < Attach.MAX_IMAGES; index += 1) {
    big.push(imageFile("image/png", 2 * 1024 * 1024));
  }
  await controller.addFiles(big.slice(0, 1));
  assert.strictEqual(controller.count(), 1);
  notices.length = 0;
  const rest = await controller.addFiles(big.slice(1, 3));
  assert.strictEqual(rest.length, 0, "2 MiB + 2 MiB + 2 MiB exceeds the total");
  assert.deepStrictEqual(notices, ["images are too large: at most 4 MiB in total"]);

  // Sending takes the images and clears the strip; a late read cannot bring
  // a sent image back.
  const pending = controller.addFiles([imageFile("image/png", 32)]);
  const taken = controller.take();
  assert.strictEqual(taken.length, 1, "take returns the pending images");
  assert.strictEqual(taken[0].mime_type, "image/png");
  assert.strictEqual(controller.count(), 0, "take clears the draft");
  await pending;
  assert.strictEqual(controller.count(), 0, "a read finishing after take adds nothing");

  // Disabling mid-draft drops the draft: no hidden images on a lost capability.
  controller.setEnabled(true);
  await controller.addFiles([imageFile("image/png", 32)]);
  assert.strictEqual(controller.count(), 1);
  controller.setEnabled(false);
  assert.strictEqual(controller.count(), 0, "disabling drops pending images");

  // Restoring a draft after a failed send keeps canonical entries only.
  controller.setEnabled(true);
  controller.add([{ mime_type: "image/webp", data: "UklGRg==" }, { mime_type: "text/html", data: "x" }]);
  images = controller.images();
  assert.strictEqual(images.length, 1);
  assert.strictEqual(images[0].mime_type, "image/webp");

  console.log("attach: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
