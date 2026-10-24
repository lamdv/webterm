/* Smoke test for the canonical pi view (webterm/static/pi-view.js). */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const modulePath = path.join(__dirname, "..", "webterm", "static", "pi-view.js");

class Base {
  constructor(session, hooks) {
    this.session = session;
    this.hooks = hooks;
  }
  static create(session, hooks) {
    const View = this.prototype instanceof Base ? this : Base;
    return { view: new View(session, hooks), kind: "base" };
  }
}

const registrations = [];
global.window = {
  WebtermAgentView: Base,
  WebtermAgentViews: {
    register(name, view) {
      registrations.push({ name, view });
    },
  },
};
require(modulePath);

assert.strictEqual(registrations.length, 1, "loading the module registers one view");
const [{ name, view }] = registrations;
assert.strictEqual(name, "pi", "it replaces the pi adapter entry");
assert.strictEqual(view.transport, "canonical", "it speaks the canonical envelope");
assert.strictEqual(view.label, "pi", "the tab label stays pi");

const PiAgentView = window.WebtermPiAgentView;
assert.strictEqual(typeof PiAgentView, "function", "the subclass is exported");
assert.strictEqual(
  Object.getPrototypeOf(PiAgentView),
  Base,
  "the pi view extends the shared agent view class"
);

const built = view.create({ id: "1-1", title: "pi" }, { onCommand: () => {} });
assert.strictEqual(built.kind, "base", "it builds through the base class");
assert.ok(built.view instanceof PiAgentView, "the subclass instance is used");
assert.strictEqual(built.view.session.id, "1-1", "the session is passed through");

console.log("pi view smoke test passed");
