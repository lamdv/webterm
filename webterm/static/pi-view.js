/* The pi view, rebuilt on the shared agent base class.
 *
 * Opt-in for now (`?piui=canonical`): the legacy `pi.js` view stays the
 * default until this one has been clicked through in a browser.  Enabling it
 * registers `pi` in the adapter registry with the canonical transport, so
 * `app.js` builds canonical `agent`-envelope hooks and the base view renders
 * pi's transcript, queue, palette, declared commands (fork/clone/export/tree),
 * help, chips and widgets.
 *
 * Nothing pi-specific is hardcoded here: the palette entries and the help
 * list arrive in the resync (the server declares them from the pi codec), and
 * everything else is the base class.  A pi-only addition belongs in this
 * class as an override of the documented hooks:
 *
 *   adapterPaletteEntries()  extra palette rows, sent as declared commands
 *   handleAdapterEvent()     pi-only event names
 *   applySnapshotExtra()     extra state from the resync payload
 *   headerExtras()           extra header nodes
 *
 * Once this view reaches parity, `pi.js` is deleted, this module registers
 * unconditionally, and the `piui` switch goes away (#13).
 */
"use strict";

(function () {
  let enabled = false;
  try {
    enabled = new URLSearchParams(window.location.search).get("piui") === "canonical";
  } catch (_error) {
    enabled = false;
  }
  if (!enabled) return;

  const Base = window.WebtermAgentView;
  const registry = window.WebtermAgentViews;
  if (typeof Base !== "function" || !registry) return;

  class PiAgentView extends Base {
    static create(session, hooks) {
      return super.create(session, hooks);
    }

    // pi has no extras yet: the declared `fork`/`clone`/`export`/`tree`
    // commands, the help list, and the extension status/widgets already ride
    // the canonical protocol, so the base class renders them.
  }

  window.WebtermPiAgentView = PiAgentView;
  registry.register("pi", {
    transport: "canonical",
    label: "pi",
    create: PiAgentView.create.bind(PiAgentView),
  });
})();
