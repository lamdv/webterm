/* The pi view, built on the shared agent base class.
 *
 * It registers `pi` in the adapter registry with the canonical transport, so
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
 * This replaced the legacy `pi.js` view (#13); the shared base renders
 * everything, and pi-specific additions would arrive through the hooks below.
 */
"use strict";

(function () {
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
