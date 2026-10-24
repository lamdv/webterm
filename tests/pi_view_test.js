/* Smoke test for the opt-in canonical pi view (webterm/static/pi-view.js). */
"use strict";

const assert = require("node:assert");
const path = require("node:path");

const modulePath = path.join(__dirname, "..", "webterm", "static", "pi-view.js");

function load(search) {
  const registrations = [];
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
  global.window = {
    location: { search },
    WebtermAgentView: Base,
    WebtermAgentViews: {
      register(name, view) {
        registrations.push({ name, view });
      },
      get(name) {
        const found = registrations.filter((entry) => entry.name === name);
        return found.length ? found[found.length - 1].view : null;
      },
    },
  };
  delete require.cache[require.resolve(modulePath)];
  require(modulePath);
  return { registrations, Base };
}

// ------------------------------------------------------------- without flag
{
  const { registrations } = load("?agentui=1");
  assert.deepStrictEqual(registrations, [], "the pi view stays legacy without the flag");
  assert.strictEqual(global.window.WebtermPiAgentView, undefined, "no class is exported");
}

// ---------------------------------------------------------------- with flag
{
  const { registrations, Base } = load("?piui=canonical");
  assert.strictEqual(registrations.length, 1, "the opt-in registers one view");
  const [{ name, view }] = registrations;
  assert.strictEqual(name, "pi", "it replaces the pi adapter entry");
  assert.strictEqual(view.transport, "canonical", "it speaks the canonical envelope");
  assert.strictEqual(view.label, "pi", "the tab label stays pi");

  const PiAgentView = global.window.WebtermPiAgentView;
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
}

// ------------------------------------------------- other values stay legacy
{
  const { registrations } = load("?piui=1");
  assert.deepStrictEqual(registrations, [], "only the explicit value opts in");
}

console.log("pi view smoke test passed");
