# Frontend

The browser client: plain HTML/CSS/JS with no build step. It renders one
xterm.js terminal per server session, manages tabs and launchers, and keeps a
single WebSocket in sync with the server. This document explains the asset
layout and the structure of `app.js`. For the server side see
[backend.md](backend.md); for the big picture see
[architecture.md](architecture.md).

## Assets

| File | Role |
|---|---|
| `webterm/static/index.html` | Document shell and DOM skeleton. |
| `webterm/static/style.css` | All styling and CSS custom properties. |
| `webterm/static/app.js` | Client logic (a single IIFE). |
| `webterm/static/highlight.js` | Safe fenced-code and diff highlighter, exposed as `window.WebtermHighlight`. |
| `webterm/static/pathcomplete.js` | Literal `@path` completion popup shared by the chat composers, exposed as `window.WebtermPathComplete`. |
| `webterm/static/attach.js` | Composer image input: paste, drag/drop, thumbnail strip, exposed as `window.WebtermAttach`. |
| `webterm/static/pi.js` | Structured pi chat view and event reducer, exposed as `window.WebtermPi`. |
| `webterm/static/agent.js` | The generic canonical agent chat view, exposed as the `window.WebtermAgentView` class (with the `window.WebtermAgent.create` factory alias); also registers itself as the base of the adapter view registry. Enabled for pi with `?agentui=1`. |
| `webterm/static/views.js` | Adapter view registry (self-registration + on-demand module loading), exposed as `window.WebtermAgentViews`. |
| `webterm/static/find.js` | Client-side transcript search bar shared by both chat views, exposed as `window.WebtermFind`. |
| `/config.js` (generated) | Runtime config, served by the server, assigns `window.WEBTERM_CONFIG`. |

### Load order

`index.html` loads, in order:

1. `xterm.css` from jsDelivr (pinned, `integrity`, `crossorigin`).
2. `/style.css`.
3. `xterm.js` from jsDelivr (pinned, `integrity`, `crossorigin`).
4. `/config.js` (generated).
5. `/highlight.js`.
6. `/views.js`.
7. `/find.js`.
8. `/pathcomplete.js`.
9. `/attach.js`.
10. `/pi.js`.
11. `/agent.js`.
12. `/app.js`.

Because `app.js` runs after the CDN script and config, it can assume
`window.Terminal` and `window.WEBTERM_CONFIG` exist. The pinned CDN URLs carry
Subresource Integrity hashes; **if you touch the asset tags, preserve or
deliberately update those hashes.** The README explains how to vendor the files
locally for offline use.

`style.css` exposes the palette as `:root` custom properties: `--bg`,
`--bg-alt`, `--bg-elevated`, `--border`, `--text`, `--text-dim`, `--accent`,
`--accent-dim`, `--danger`, `--ok`, `--warn`. The terminal's own theme is
separate (`TERMINAL_THEME` in `app.js`) and must stay in sync visually.

## `app.js` structure

The whole file is one `"use strict"` IIFE. It is organized into commented
sections:

| Section | Responsibility |
|---|---|
| Top-level state | Element refs, `terminals`, `subscribed`, `activeId`, `socket`, reconnect state. |
| Sidebar | Width/clamp/persist, drag-to-resize, collapse/expand, responsive auto-collapse, `?sidebar=` handoff. |
| Socket | `connect`, `send`, `setStatus`, reconnect backoff. |
| Control plane | `handleControl`, `reconcileSessions`, `ensureTerminal`. |
| Tab / view | `renderTabs`, `activate`, `startRename` (inline tab rename). |
| Pi view | `ensurePiView`, pi trust/resume dialogs, `createPiSession` and `createPiResumeSession` launchers, export download. |
| Resize | `fit`, `cellSize`, `domCellSize`, `refitAll`. |
| Actions | Launchers, `createSession`, `renderEmptyChoices`, session-type switch, keyboard shortcuts. |
| Boot | Feature check, initial render, `connect()`. |

### Sidebar

- **Resizable** — drag `#sidebar-resizer`, or focus it and use Left/Right
  (Shift for larger steps); double-click resets to 220px. The width is clamped
  to 150–640px and `window.innerWidth - 200`, and persisted in
  `webterm.sidebarWidth`.
- **Collapsible** — the header button collapses it, `#sidebar-expand` brings it
  back, and `Ctrl+Shift+B` toggles. The preference is persisted.
- **Responsive** — below `NARROW_VIEWPORT` (640px) the sidebar is collapsed no
  matter what the stored preference says, and choosing a shell collapses it
  again, leaving only the expand button. Widening the window restores the
  preference.
- **Desktop width handoff** — the desktop picker appends `?sidebar=<px>` when it
  navigates to a server; the client adopts that as the width, persists it, and
  strips the parameter with `history.replaceState` so a reload falls back to the
  stored value.

### State

- `terminals: Map<sessionId, {term, wrapper}>` — one xterm instance and its
  DOM container per known session.
- `subscribed: Set<sessionId>` — which sessions this socket has asked for.
- `activeId` — the visible session, persisted to `localStorage`.
- `sessionsMeta` — the latest `sessions` list from the server.
- `userLaunchers` — user-defined launcher pills, persisted.
- `CONFIG = window.WEBTERM_CONFIG || {}` — `fontFamily`, `fontSize`,
  `launchers`, `pi` (`available`, `cwd`, `sessionDir`, `version`,
`testedVersion`), and `agents` (structured
  profiles; the `llama` entry drives the browser's llama chat launcher).
- Reconnect state: `socket`, `reconnectDelay`, `closedByPage`,
  `activateNext`, `restarting`. Each terminal entry also tracks `offset`, the
  number of output bytes it has rendered.

`DEFAULT_FONT_FAMILY` is a Nerd Font aware stack; `CONFIG.fontFamily` overrides
it and `CONFIG.fontSize` defaults to 13.

### Socket and reconnect

`connect()` opens `ws(s)://<host>/ws`, sets `binaryType = "arraybuffer"`, and:

- on open, resets backoff, clears `subscribed`, and sends
  `{"type":"list"}`. Terminals are **not** reset: reconnect resumption is
  offset-based (see below), so alternate-buffer and mouse-tracking state
  survives a dropped socket.
- on message, dispatches strings to `handleControl` and binary payloads to
  `handleOutput`.
- on close, retries with exponential backoff capped at 8 s, unless the page is
  unloading (`closedByPage`). If `restarting` is set, it keeps showing
  "restarting…" instead of the disconnected status.

`send()` is a no-op unless the socket is `OPEN`.

### Control plane

`handleControl` handles `sessions`, `attached`, `exit`, and `restarting`. The
latter sets `restarting` and the status text; the flag is cleared on the next
`socket.onopen`.

`reconcileSessions(sessions)` is the core reconciler:

1. For each server session, `ensureTerminal` and, if not already subscribed,
   subscribe with `since: entry.offset` so the server replays only the bytes
   this terminal has not seen.
2. Dispose terminals and wrappers for sessions the server no longer reports.
3. Re-render tabs.
4. Choose the active session: if a create just happened (`activateNext`), switch
   to the newest; otherwise keep or fix `activeId`. An unchanged active session
   is only re-activated when its wrapper was created in this pass, never for a
   metadata-only update.

It diffs against `knownBefore` so it can detect newly created sessions even
though the server sends a full list.

`ensureTerminal(session)` creates the wrapper, constructs xterm, and wires
`onData` → `{"type":"input",...}` and `onResize` →
`{"type":"resize",...}`. It hides `window.OffscreenCanvas` during construction
and `open()` to force xterm's reliable DOM-based font measurement on browsers
lacking `TextMetrics.fontBoundingBoxAscent/Descent`; without this the terminal
can get stuck at 80 columns.

`handleOutput` parses the binary frame: first byte is the session-id length,
the next bytes are the UTF-8 id, then an 8-byte big-endian absolute offset, then
the terminal output. `applyOutput` writes the chunk at that offset into the
session's `term` and advances `entry.offset`:

- `offset === entry.offset` — the normal path (live output or an exact resume);
  write and advance.
- `offset < entry.offset` — overlapping replay; skip the already-seen prefix.
- `offset > entry.offset` — the missed bytes were evicted from the server's
  scrollback while disconnected, so `term.reset()` first and continue from the
  retained tail. (This is the only path that clears terminal state.)

Unknown ids are ignored.

`handleExit` writes a dim "[process exited]" line and unsubscribes.

### Tabs and view

- `renderTabs` rebuilds `#sessions` from `sessionsMeta` as a **tree**: sessions
  are ordered by `parent_id` (`orderedSessions`), children are indented with a
  `--depth` custom property, and a child whose parent is gone falls back to the
  root so nothing disappears. Trailing chips read left-to-right by how transient
  they are: cost (`$0.012`, agent sessions only), child count, a pulsing
  activity dot, a `!` for `needs_input`, and an `exit N` chip once dead. The
  activity dot has two strengths. It breathes faintly (opacity `0` → `0.5`)
  while the agent is thinking or running a tool (`session.activity`), and keeps
  the bright expanding ring while text is being written — and while an answer
  arrived that this tab has not been opened for. That last state is client-side:
  `answeredSeen` maps a session id to the `answers` count the user has looked
  at, seeded on first sight (so a reconnect does not light up the sidebar) and
  reset whenever the tab becomes active. The
  right-most control is always the close button: tagged sessions
  (`session.agent`, else `pi` for the pi adapter, else the adapter name) render
  it as a
  `.tab-tag-close` pill that shows the tag and swaps it for a `×` on hover,
  while untagged shells keep the plain `.tab-close` `×`. A click sends
  `{"type":"kill"}` (which cascades to the session's descendants server-side).
  Each tab has an inline-rename title (double-click, or a ~600 ms long-press /
  right-click on touch, via `attachLongPress` — both close controls are
  excluded).
- A session created by an agent does **not** steal focus: only a session the
  user created (`activateNext`) is activated on arrival.
- `activate(id)` persists the choice, toggles the `active` class on wrappers,
  updates tab highlighting, and fits/focuses the terminal on the next animation
  frame plus a 50 ms follow-up (needed when a terminal is first made visible).
  It is not a refresh: the periodic `sessions` broadcasts that a streaming
  agent causes must not re-activate the current tab, or every state change
  would pull the caret back into its composer while the user reads or types
  elsewhere.

### Structured views

Every structured session reports `kind: "agent"`. Which view renders it is
decided by an **adapter view registry** (`static/views.js`), not by a list of
backend names in `app.js`:

```js
window.WebtermAgentViews.register("pi", {transport: "pi", label: "pi", create});
```

`create(session, hooks)` returns the view handle (`element`, `handle`,
`setExited`, `activate`, `destroy`). `transport` selects the envelope the view
speaks: `"canonical"` (the shared `agent` command and `agent_event` stream) or
`"pi"` (the legacy raw pi envelope), and it is what `app.js` builds hooks for
and what decides whether a subscription asks for `agent_api`. The view
registered under `""` is the base every adapter falls back to; the generic
canonical view (`agent.js`) registers it.

The server advertises adapters in `/config.js` (`agentAdapters`, see
[backend.md](backend.md)); `WebtermAgentViews.preload()` injects any advertised
`view` module that the page has not loaded, so **adding a backend means adding
a module and a registry/config entry, not editing `app.js` or `index.html`**.
An adapter with no module, or whose module fails to load, uses the base view,
and the declared commands in its resync keep its features reachable.

`reconcileSessions` resolves `session.adapter` through the registry; `?agentui=1`
forces the base canonical view for every adapter, which is how pi is previewed
on the shared renderer. Both kinds are routed away from xterm by their `kind`.
`agent.js` is the long-term renderer and receives new chat features; the pi
module below is legacy until its UI is ported onto the base class (#13).

Both chat views mount the same **transcript search** (`static/find.js`) above
their message list: Ctrl/Cmd+F inside the view, or the `find` button in the
header, opens a bar with a query field, a `3 / 12` match count, next/previous
buttons and a one-line snippet. Search runs over the text the view rendered,
not over the DOM, so the count matches what the reader can see; the hit is
marked inside the bar's snippet and the revealed entry gets a transient
outline, because both renderers rebuild entry nodes while a reply streams and
an in-place mark would vanish on the next delta. Revealing an entry opens every
collapsed block under it (tool cards, thinking), and when the query has no
match but earlier history is unloaded, the bar offers **search earlier
messages**, which fetches one page and re-runs the query. A live, not yet
finished message is searchable only after the adapter appends it as an entry,
so the count never points at a node that has no stable id. The bar stops
keyboard events it handles (Enter, Shift+Enter, Esc) so a search never sends a
prompt or aborts a run.

### Pi view

`ensurePiView` creates `window.WebtermPi.create(session, hooks)` for pi-backed
sessions (`session.adapter === "pi"`), which keep this view unless `?agentui=1`
asks for the canonical one. The view is hydrated by server `pi_resync` snapshots
and then consumes `pi_event` records; it owns no persistence. Hooks map to the
generic RPC envelope (`onCommand`), an explicit snapshot refresh (`onRefresh`),
backwards transcript paging (`onHistory`), and `pi_export` (`onExport`). A
snapshot carries only the newest entries plus `hasMore`/`firstEntryId`; the
view renders a **Load earlier messages** control when older entries exist and
prepends each `pi_history` page while holding the current scroll position. A
resync invalidates in-flight page requests. The layout is a title bar
(`.pi-header`: title plus extension status chips), the transcript, the composer
and a bottom status bar (`.pi-footer`: idle/working, model, thinking level,
message and queue counts). The composer stretches its textarea to the full
height of the action column (Send above Stop), and the Send button keeps one
fixed height whether it reads `Send` or `Follow up`; the hold-to-steer gesture
is described by its tooltip and the busy placeholder. Toasts float above the
composer from a zero-height host, not from a hardcoded offset. The composer
recognizes webterm built-ins
(`/new`, `/compact`, `/model`, `/provider`, `/reload`, `/fork`, `/clone`,
`/tree`, `/export`, `/limits`) and merges pi's `get_commands` results for
extension/template/skill commands. `/model` and `/provider` are webterm-side
pickers over `get_available_models`; selecting an entry sends `set_model` and
updates the bottom status bar's model label from the response without a full
resync. `/limits` opens a modal naming the TUI-only surfaces RPC cannot
reach, and a draft that starts with one of those commands (`/login`,
`/settings`, `/trust`, and friends) is refused with a banner pointing at the
same modal instead of being sent to the model as a prompt; the draft stays in
the composer so it can be edited or copied.

Model text is rendered through `WebtermHighlight.renderText()`, which splits
fenced code blocks into DOM-only highlighted `<pre>` nodes. Tool arguments are
highlighted as JSON, and tool output is classified as a unified diff when
possible (`@@`, `+++`/`---`, `+`/`-` lines) with line-level colors. Unknown
languages fall back to plain preformatted text. The highlighter never uses
`innerHTML`; token spans are built with `textContent`. Token arrays are cached
by language+content to keep streaming re-renders cheap, and the transcript
keeps its scroll position unless the viewer is already near the bottom.

Before a pi process starts, the trust dialog always asks whether project
settings/extensions may load. The resume dialog lists persisted sessions via
`list_pi_sessions`, then asks the same trust question for the selected cwd
before sending `create` with `resume`. Both are reachable from the `pi chat`
launcher (long-press/right-click for resume), from the `pi chat` session type in
the new-session popover, and from the empty-state buttons.

Extension UI is server-owned: `pi_ui_request` dialogs are shown one at a time
and answered with `pi_ui_response`; `pi_ui_resolved` dismisses them for every
viewer. Fire-and-forget requests render as toasts (`notify`), keyed header
chips (`setStatus`), pre-composer widgets (`setWidget`), the chat header title
(`setTitle`), and composer prefill (`set_editor_text`). A pending dialog adds a
`!` badge to the session tab and `needs_input` to session metadata.

#### Literal `@path` completion

Both chat composers (the pi view and the canonical agent view) bind
`pathcomplete.js`, which completes file paths as **literal text**. pi running
over RPC has no `@file` expansion, so nothing is hidden behind the token: the
model receives the absolute path exactly as inserted.

- **Trigger** — the caret sits in an `@` token that starts the message or
  follows whitespace, contains no whitespace, and ends at the caret. `@` alone
  lists the session's `cwd` (the server's default when the session has none);
  everything after `@` up to the last `/` names the directory to list (relative
  to that cwd, or absolute/`~`), and the remainder filters entry names
  case-insensitively. `@src/` therefore descends into `src`.
- **Listing** — a `list_entries` request (`{path}`) is answered from the same
  synchronous scan as `list_dirs`, but with files: directories first, then
  files, sorted case-insensitively and capped at 500 entries with `truncated`.
  The picker renders at most 50 matches, building them with `textContent`.
- **Keys** — ArrowUp/ArrowDown move, Enter/Tab accept, Escape closes the popup
  only (it never aborts the run or clears the draft), and Shift+Enter stays a
  newline. While the popup is open plain Enter accepts and never sends: the
  composer only sees a key the picker declined, and Ctrl/Cmd+Enter still
  steers.
- **Insertion** — accepting replaces the whole `@token` with the absolute path
  from the server. Accepting a directory keeps the `@` and appends `/`, so the
  picker stays open to descend further. A path containing whitespace is wrapped
  in double quotes with `\`, `"`, `$` and the backtick backslash-escaped, so
  the model and a shell both read it as one word. Entry names containing a
  newline are never offered, which keeps the mention on one line.

### Canonical agent view

`agent.js` renders every native structured agent (an adapter other than
`"pi"`, such as `llama`) and pi-backed sessions when `?agentui=1` switches
them over. The legacy `pi.js` view remains the pi default; native agents have
no legacy channel, so `app.js` subscribes them with `"agent_api":true` and
routes `agent_resync` / `agent_event` / `agent_dialog_*` to this view without
the query flag.

`app.js` drives the view through the canonical `agent` command envelope:
`prompt`, `steer` (busy + Ctrl/Cmd+Enter), `follow_up` (busy + Enter), `abort`,
`dialog_response`, and the generic `onCommand` hook used by the controls. The
view never sees an adapter's native dialect; the server maps each canonical
command onto the codec. Its composer binds the same literal `@path` completion
as the pi view, fed by an `onListEntries` hook over `list_entries`.

The view keeps a canonical transcript (entries from `agent_resync`, then live
`message.*` events), tool execution state keyed by `call_id`, usage/model
status, and a server-owned dialog panel. It renders text, thinking, tool cards,
images as placeholders, and user/assistant/tool entries. When the snapshot
reports `has_more`, a **Load earlier messages** button fetches one backwards
page with `agent.get_history` and prepends it without moving the reader; it
disappears once the history is exhausted.

A controls row above the transcript is capability-driven. When
`models.list` is advertised it fills a model picker from `list_models` and
sends `set_model`; when `models.thinking` is advertised a thinking picker uses
the adapter's `models.levels` (falling back to `off`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`) and sends `set_thinking`; when
`system_prompt` is advertised a panel edits the prompt and sends
`set_system_prompt` (an empty value clears it). Each control tracks the value
the adapter accepted in the view's local `state.meta`, so the header and the
status bar update without a resync. Adapters that do not advertise a control
never see it, which is why the llama chat has all three and pi's canonical view
does not.

A past turn carries a small **edit** button in its entry header: user messages
when the adapter advertises `edit_message`, assistant answers when it advertises
`edit_answer` (the llama chat does both; pi's canonical view does neither).
Clicking it swaps that entry's body for a textarea holding the message or
answer text, with a save button and **Cancel**; Enter saves and Shift+Enter
adds a line. The save button reads **Save & resend** for a user message and
**Save** for an answer, because only the former starts a run. Saving sends one
`{type:"edit_message",entry_id,message}` command with the trimmed text and
closes the editor when the adapter's `transcript.truncated` event arrives —
the transcript is server-owned, so the response only reports the outcome
(`message edited; N later message(s) dropped`, or `answer edited; ...`). The
event drops every entry from `entry_id` on, and the adapter's new entry then
arrives as a `message.finished`, which replaces any optimistic copy exactly
like a fresh prompt. An empty draft is refused locally, an edit while a run is
streaming is refused with a warning instead of being sent, a rejected edit
keeps the editor open with the adapter's `error`, and a resync or a truncation
that removes the entry closes the editor. Without the matching capability the
button never exists.

Above the textarea, the same panel carries a saved-prompt library when the
adapter advertises `prompt_library` (`div.agent-system-library`): a
`select.agent-prompt-pick` whose first option is the `saved prompts…`
placeholder followed by the `list_prompts` names sorted by name, an inline
**Save to library** action and a **Delete** action. The list is fetched lazily
— on the first open of the panel, never at session start — and re-fetched on
every later open and after every save or delete. Picking a name is **fill
only**: it copies that prompt's text into the textarea and sends nothing, so
applying it is still the panel's **Save** button (`set_system_prompt`).
**Save to library** reveals an inline name field (prefilled with the currently
selected name, else empty) plus Save/Cancel; the request carries the textarea's
text and the typed name, while the reply's normalized `prompt.name` drives the
selection and the `saved prompt: <name>` notice. A rejected name or text (bad
name, empty or over 16384 characters) surfaces the adapter's `error` through
`showNotice(..., "error")` and leaves the field open to be corrected.
**Delete** is enabled only while a saved prompt is selected and is two-step:
the first click arms it as `Delete <name>?`, a second click sends
`delete_prompt`; arming resets on panel close, on a selection change, and after
about four seconds. A delete refreshes the list, clears the selection, and
notices `deleted prompt: <name>`. Without `prompt_library` the whole row is
hidden, no prompt command is ever sent, and the panel is exactly its
prompt-only self.

Queued input is visible, not hidden state. A canonical `queue.updated` event
(`{steering, follow_up}`) renders one chip per pending message above the
composer, labelled `steer` or `follow-up`, showing a truncated copy of the text
with the full message in the chip's `title`; the footer adds `N queued`. A
**Clear** action sends `clear_queue`, drops the chips, and appends the text the
adapter returns (`steering`/`followUp`, with the canonical `follow_up` spelling
also accepted) to the composer, so queued work is never silently lost. While a
run is busy, Esc and the **Stop** button take the same path: `clear_queue`
first, restore the returned text, then `abort`. When idle, Esc aborts nothing;
with the slash palette open it only closes the palette.

Typing `/` at the start of the composer opens a filterable slash-command
palette (ArrowUp/ArrowDown move, Enter or Tab accepts, Escape closes). Enter
with the palette open accepts the active entry and never sends the draft as a
prompt. The core entries are capability-gated and map onto controls the view
already has: `/compact` (adapter `compaction`) sends `{"type":"compact"}`,
`/model` (`models.set`) focuses the model picker, `/thinking`
(`models.thinking`) focuses the thinking picker, and `/system`
(`system_prompt`) toggles and focuses the system-prompt panel. After them the
palette appends one entry per adapter-declared command, and last `/help` (see
below).

#### Declared commands

A backend's own operations are not view features. `agent_resync` carries the
adapter's `commands` manifest and its `help` list (both absent for an adapter
that has neither), and the view renders that manifest instead of hardcoding
names:

```json
{"name":"export","title":"Export session","description":"Download HTML or JSONL",
 "args":[{"name":"format","type":"choice","title":"Format","required":true,
          "choices":[{"value":"html","label":"HTML"}]}],
 "danger":false}
```

Each declared command becomes one palette entry, listed after the core
built-ins in manifest order with its `title` (also the row's tooltip) and
`description`, and a `danger` chip when the adapter marks it destructive.
Accepting one never changes the view's own state machine:

- **No args** — the entry sends `{"type":"command","name":…}` immediately
  (`args` is omitted).
- **With args** — a small generic form opens (`div.agent-command-form`). It has
  one control per arg, chosen by `type` only: a text `<input>` for `string`, a
  `<select>` of `choices` (`{value,label}`) for `choice`, a checkbox for
  `boolean`; an unknown type falls back to a text input. The arg's `title` is
  its label, its `description` the help line under the control, and a required
  arg gets an `*`.

**Run** sends `{"type":"command","name":…,"args":{…}}` with the typed and
selected values and closes the form; Enter in a field submits, Escape or
**Cancel** closes without sending. Validation is local: an empty required arg
blocks the submit with `Required: <label>` and keeps the form open, while an
empty optional arg is simply left out of `args`. A boolean is always sent, so a
checkbox that was never ticked reads as `false`.

The form holds no backend knowledge — no field name, choice, or default is
written in `agent.js` — so a new arg'd command needs no view change at all.

#### Help dialog

`help` entries (`{name,reason}`) are the features the adapter says **cannot**
work over the protocol. While that list is non-empty the palette ends with
`/help`, which opens a dialog listing one `name — reason` row per entry; with no
entries the palette entry and the dialog do not exist. This replaces the
hardcoded `/limits` gap list of the legacy view, so a missing feature reads as
the adapter's own documented gap instead of a broken control.

#### Render hints

The response to a declared command comes back as `data`, which the view routes
through a small fixed vocabulary (one dialog at a time; a resync closes it):

| `data` | Rendered as |
|---|---|
| `{"notice":{"level","text"}}` | the banner the view already uses for notices, with the level as its class |
| `{"download":{"url","filename","mime_type"}}` | a link to the server's authenticated route, labelled with `filename` (else `download`), carrying `download` and the MIME type below it |
| `{"tree":{"nodes":[…],"leaf_id"}}` | a dialog of collapsible nodes: `label`, `detail`, a `current` chip on `leaf_id`, nested children rendered the same way (top-level branches start open), and one button per `actions` entry — a button sends that action's `command` through exactly the same path as a palette entry |
| `{"text":"…"}` | a `<pre>` block; the text is never parsed as markup |
| `{"refresh":true}` | `get_snapshot` through the view's command hook; the server also broadcasts `agent_resync`, which is what actually rehydrates the transcript |
| anything else | a collapsible JSON `<details>` card, so an unknown shape stays visible instead of being dropped |

A `success:false` answer arrives as a rejection from the command hook and takes
the existing notice path (`showNotice(…, "error")`), so the adapter's own
wording is what the human sees. Empty data renders nothing. The renderer knows
only these shapes: it cannot render a backend's meaning, which is what keeps
the manifest — not the view — the place where a feature is described.

The view therefore carries no backend vocabulary: names, titles, args, choices,
node labels, and results all arrive from the adapter, and the only fixed words
in `agent.js` are the four core slash commands and the `help` entry. `pi.js`
keeps its own hardcoded command list because it speaks pi's dialect directly;
the canonical view does not grow with a backend.

#### Transcript rendering

Live entries are patched in place rather than re-rendered. `render()` keeps one
record per entry id (`renderedEntries`) holding the entry's nodes and one record
per content block, so a streaming delta re-fills only the blocks whose text or
signature changed; the live assistant message keeps its DOM (and any text
selection) across deltas. A rebuild happens only when an entry's **structure**
changes — role, error/optimistic flags, or the kinds of its blocks — and even
then `reconcileChildren` removes and re-appends instead of rebuilding the whole
transcript. UI state that must outlive a rebuild lives outside the DOM: a
thinking block's open flag is keyed by entry id plus block index in
`blockOpen`, so it survives streaming, the `message.finished` event that moves
the live entry into the transcript, and prepended history pages. Scroll
position is re-anchored after an older page is prepended so the reader keeps
their place.

#### Extending the view

`agent.js` exports the shared view as a class, `window.WebtermAgentView`, and
registers it as the base view (the `""` entry) of the registry. A backend that
needs UI the shared renderer does not have subclasses it instead of forking the
file:

```js
class LlamaView extends window.WebtermAgentView {
  headerExtras() {
    return [statusChip(this.session)];
  }
}
window.WebtermAgentViews.register("llama", {
  transport: "canonical",
  // The registry calls create() unbound, so name the subclass explicitly.
  create: (session, hooks) => LlamaView.create(session, hooks),
});
```

The subclass surface is four methods, each a no-op by default, so the base view
behaves exactly as if it had no extension points:

| Hook | Called |
|---|---|
| `adapterPaletteEntries()` | while the slash palette is built, after the core built-ins and the adapter's declared commands and before `/help`. Entries use the declared-command record shape, and an entry carrying `declared: true` sends its canonical `command` when accepted. |
| `handleAdapterEvent(name, event)` | for every `agent_event` whose name the canonical reducer does not know. Known events, the replay/cursor filter and the render that follows stay the base class's job. |
| `applySnapshotExtra(payload)` | on every `agent_resync`, after the base state, capabilities, declared commands and history cursor are applied, immediately before the render. |
| `headerExtras()` | once during construction; returns nodes appended to the header between the title/meta text and the find button. |

Everything else — the transcript renderer, editing, queue chips, dialogs and
declared commands — belongs to the base class, so a subclass adds UI without
drifting from the shared behaviour. An adapter with no registered view, or
whose module fails to load, still falls back to the base view.

`window.WebtermAgent.create` stays as a factory alias for the class, and
`create(session, hooks)` builds the class it is invoked on, so a subclass
inherits the factory.

### Composer images

Both structured views attach images through `window.WebtermAttach`
(`attach.js`), a single controller per composer shared by `pi.js` and
`agent.js`. It owns paste (`paste` on the textarea, `clipboardData.files`) and
drag/drop onto the composer, and a thumbnail strip (`div.pi-attach-strip`) that
sits above the textarea with one remove button per image. Images are held as
canonical `{mime_type, data}` entries with raw base64; the pi view converts
them to pi's `ImageContent` shape only when it builds the raw RPC command,
while the canonical view passes them to `prompt`, `steer`, and `follow_up`
unchanged.

Input is capability-gated: the view calls `setEnabled()` from the snapshot's
`attachments.images` (pi's `pi_resync` now carries the capability map, just
like `agent_resync`), so an agent that does not accept images has no attach
affordance and a stale draft is dropped when the capability disappears.
Sending takes the strip's images and clears them; a rejected command restores
the draft so nothing is lost. The browser checks the same limits as the server
(4 images, 3 MiB each, 4 MiB per message, png/jpeg/webp/gif) only to answer
locally with the server's wording (`too many images`, `image too large`,
`unsupported image type`, `images are too large`); the server re-validates and
is the authority. Pending images live in memory for the life of the view:
nothing is written to `localStorage`, so base64 blobs never enter the quota,
and a page reload or a view teardown drops them.

### Resize and fitting

The `fit` routine is deliberately defensive because xterm's layout can be
unmeasurable right after creation:

- It reads the viewport size; if zero, it gives up.
- `cellSize` prefers xterm's own renderer metrics, falling back to measuring a
  32-`W` span with the same font (`domCellSize`).
- For the active terminal it calibrates cell width against what xterm actually
  rendered (`scrollWidth / cols`) so the last column is not clipped.
- It retries with `setTimeout` while fonts/rendering settle.
- `refitAll` is triggered on `window.resize` (debounced), and a
  `ResizeObserver` watches the terminal area so sidebar changes and scrollbars
  also trigger a refit for *all* sessions, keeping background PTYs correctly
  sized.

### Virtual keyboard and touch layout

On phones the browser keeps the *layout* viewport at full height while the
virtual keyboard is open and only shrinks the *visual* viewport
(`window.visualViewport`). A `100vh` app therefore hides the terminal's last
rows behind the keyboard. `syncViewport()` closes that gap:

- It writes `--app-height` (the visual viewport height) and
  `--viewport-offset-top` (the visual viewport's `offsetTop`) as custom
  properties on `:root`. `#app` uses them for its height and a `translateY`, so
  the shell always fits the visible area — including the iOS case where the
  browser pans the visual viewport to reveal a focused input. `100dvh` in the
  stylesheet is the no-JS fallback.
- It treats the keyboard as open when
  `innerHeight - (visualViewport.height + offsetTop)` exceeds 120 px. On the
  transition to open it collapses an expanded sidebar (with
  `setSidebarCollapsed(true, false)`, so the user's saved preference is
  untouched) and records that it did; on close it restores the
  sidebar only if the user has not re-opened it in the meantime. If the sidebar
  was already collapsed, nothing changes. On a narrow viewport the sidebar
  stays hidden when the keyboard closes, as it does everywhere else.
- `visualViewport` `resize`/`scroll` re-run `syncViewport()` and schedule a
  debounced `refitAll` so the PTY grid tracks the keyboard.

The chosen threshold means URL-bar changes (~50–80 px) are ignored, so the
sidebar is not collapsed by ordinary scrolling chrome.

Touch-specific styling lives in one `@media (hover: none), (pointer: coarse)`
block in `style.css`: the per-tab `×` close button is made permanently visible
(it is hover-only on desktop), rows and buttons get finger-sized hit targets,
and popover inputs are bumped to 16 px so iOS does not zoom the page when they
focus.

The chat surfaces extend the same rules in a `max-width: 700px` block: the
transcript find bar wraps with its input on its own line and drops the snippet,
so the query and the match count stay usable; modals (trust, resume, tree,
`/limits`) span the width with `max-height: calc(100dvh - 16px)` and align to
the top so the on-screen keyboard cannot push their buttons off-screen; the
header and agent control row wrap instead of overflowing; and queue chips keep
their kind label narrow. Thumbnails and find/queue buttons get touch-sized hit
targets on coarse pointers. Interactive elements also get `touch-action: manipulation` to drop the
double-tap-zoom delay. The viewport meta tag carries `viewport-fit=cover`, and
`#app` pads by `env(safe-area-inset-*)` for notched displays.

On a narrow viewport (below 640 px) the sidebar stays collapsed whatever the
stored preference says, and widening the window restores that preference. The
stored preference itself is only written when the user toggles the sidebar, so
automatic collapsing never overwrites it.

### Wheel and mouse input

xterm.js (through at least 5.5.0) drops wheel events that its line
accumulator rounds down to zero: small/high-resolution wheel and trackpad
deltas, and any wheel event while `Shift` is held. Two behaviours depend on
those events:

- Full-screen applications that requested mouse tracking (pi, btop, ...)
  receive scrolling only as SGR mouse reports, so the drops disable scrolling
  entirely.
- Full-screen applications without mouse tracking expect *alternate scroll*:
  xterm translates the wheel into cursor up/down keys, but the same zero-line
  early return skips that, so the event escapes to the browser and scrolls the
  page instead of reaching the PTY.

To work around it, each terminal wrapper installs a capture-phase `wheel`
listener. `handleWheel()` sends the application an SGR mouse report when mouse
tracking is active (`forwardMouseWheel()`, using xterm's own mouse service so
the active encoding is honoured) and otherwise sends cursor up/down keys when
the alternate buffer is active (honouring `applicationCursorKeysMode`; rounded-
to-zero deltas accumulate rather than escaping to the browser). In the normal
buffer it does nothing and lets xterm scroll the scrollback. This uses xterm
internals (`term._core._mouseService`, `coreMouseService.triggerMouseEvent`,
`viewport.getLinesScrolled`), guarded so a future xterm that changes them falls
back to stock behavior. Populated by `handleWheel()` in `ensureTerminal()`.

#### Touch drag

The same wheel gap makes full-screen TUIs unscrollable on a touch screen: when
an app turns mouse tracking on, xterm 5.5 returns early from its `touchstart`
and `touchmove` handlers, and its touch scroll only moves the viewport's
`scrollTop`, which an alternate buffer has no scrollback for. `attachDragScroll()`
(installed per wrapper) fills that gap:

- It only tracks a gesture when `mouseTracking()` is true **or** the active
  buffer is `alternate` — a normal buffer without mouse tracking keeps xterm's
  own (working) touch scrolling. Gating on the mouse mode rather than the buffer
  alone matters because an app can keep reporting the mouse while briefly
  leaving the alternate screen.
- On a one-finger `touchstart` it records the origin; the first `touchmove`
  decides the direction. A mostly-horizontal move, or one that stays inside a
  10 px dead zone, releases the gesture so taps, long-press selection and pinch
  zoom are untouched.
- Once committed it calls `preventDefault()`/`stopPropagation()` (capture,
  non-passive) so the page never pans, and accumulates vertical travel. Every
  full cell height of travel is emitted as one line via `scrollBy()`: SGR wheel
  reports while mouse tracking is on, cursor up/down keys in an untracked
  alternate buffer, or `term.scrollLines()` if the buffer changed mid-drag.
  Dragging down shows older content, matching a wheel-up.
- Multi-touch (pinch) and `touchcancel` abort the gesture.

`handleWheel()` and `attachDragScroll()` share the routing helpers
(`mouseTracking`, `sendMouseWheel`, `sendCursorScroll`, `scrollBy`), so wheel
and touch stay behaviourally identical. `html, body` set
`overscroll-behavior: none` so a drag can never turn into pull-to-refresh.

### Launchers

Launchers render as a horizontally scrollable pill row. The leading `+` pill
prompts for a shell command and an optional name, saves it, and starts it. User
launchers store the raw command string (so `&&`, pipes and globs work when it is
typed into the shell), while config launchers use an argv list.

- Config launchers come from `CONFIG.launchers` (`{label, command[]}`) and are
  not removable.
- User launchers are stored under `localStorage["webterm.launchers"]` and can be
  removed with the `×`, a right-click, or a ~600 ms long-press (`attachLongPress`).

`createSession(command, title)` sizes the new session to the active terminal's
grid and sends `{"type":"create", ...}`.

### New session

A plain click on `+ new` (or `Ctrl+Shift+N`) starts a shell immediately. A
long-press or right-click on `+ new` opens the new-session popover
(`attachLongPress`), which starts with a **session type** switch — `Shell` or
`pi chat` (offered only when `CONFIG.pi.available`). For shells the popover also
takes an optional name and an optional **precommand**; submitting calls
`createSession(precommand, name)`, so the server starts an interactive shell and
types the precommand into it — shell syntax (`&&`, pipes, globs) works and the
user lands at a prompt when it exits. Choosing `pi chat` hides the shell fields,
relabels the submit button, and swaps in a **model** picker (see below).

### Model picker

The `pi chat` branch asks the server for `pi_models` (optionally scoped to
`CONFIG.pi.cwd`, optionally `refresh`d) and renders one `optgroup` per provider
plus a leading `Default (...)` entry. The default entry names
`CONFIG.pi.defaultModel` when the server was started with
`--default-pi-model`, otherwise the model pi would start with (`payload.default`).
`↻` forces a fresh probe, which is how a `/login` or a `models.json` edit shows
up without restarting webterm.

The choice is a UI preference: it is stored under
`localStorage["webterm.piModel"]` as `{provider, id}`, restored on load, and
applied to every pi launch path (the popover, the empty-state button, and the
sidebar `pi` pill) — not just the popover. Picking `Default (...)` clears the
preference, and a preference for a model pi no longer lists is dropped the next
time a real list arrives. `createPiSession()` resolves the model *before*
opening the trust dialog and sends `provider`/`model` on create; sending neither
lets the server apply `--default-pi-model`. A resumed session never carries a
model, because the session file restores it.

When no sessions are open, `#empty-state` renders one button per session type
(`renderEmptyChoices`) instead of assuming a shell: `Start a shell`,
`Start pi chat`, and — when `CONFIG.pi.sessionDir` is set — `Resume pi…`. The
buttons are built in JS so unavailable types are simply absent.

The `⟳` button in the sidebar header asks for confirmation (restarting closes
all sessions), then sends `{"type":"restart"}`. The server acks with
`restarting`, the socket drops, and the normal reconnect loop attaches to the
freshly exec'd process.

### Keyboard

- `Ctrl+Shift+N` — new session.
- `Ctrl+Shift+B` — toggle sidebar.

### Boot

The script returns early with an error message if `window.Terminal` is missing.
It otherwise sets the connecting status, renders launchers, connects, and
schedules a refit once `document.fonts.ready` resolves (font metrics can change
after first paint).

## WebSocket protocol handling

Client to server (text JSON): `list`, `subscribe`, `unsubscribe`, `create`,
`kill`, `rename`, `input`, `resize`, `restart`. Server to client (text JSON):
`sessions`, `attached`, `exit`, `restarting`. Server to client (binary):
`[len(id)][id][offset:8][output]`. The socket URL is derived from `location` — `wss://`
when the page is served over HTTPS (the server was started with
`--tls-cert`), otherwise `ws://` — so enabling TLS needs no frontend change. See
[architecture.md](architecture.md#wire-protocol) and
[backend.md](backend.md#protocol) for the canonical description.

## `localStorage` keys

| Key | Purpose |
|---|---|
| `webterm.active` | Last active session id. |
| `webterm.sidebarWidth` | Sidebar width in px. |
| `webterm.sidebarCollapsed` | `"1"` when collapsed. |
| `webterm.launchers` | User launcher definitions (JSON). |
| `webterm.piModel` | Model chosen in the new-session picker (`{provider, id}`). |

All storage access is wrapped in `try/catch`, since storage may be unavailable
(private mode, etc.).

## Testing and linting

There is no browser test runner. The server's tests assert that `index.html` is
served and that the xterm.js CDN tag is present (`tests/test_server.py`). The
frontend is otherwise exercised manually, except for fake-DOM smoke tests run
with Node through unittest (`tests/agent_view_test.js`,
`tests/agent_queue_test.js`, `tests/agent_declared_test.js`,
`tests/app_view_test.js`, `tests/attach_test.js`, `tests/find_test.js`,
`tests/limits_test.js`, `tests/path_complete_test.js`). Keep `app.js`
dependency-free and avoid introducing a build step; anything added must run
from the served static files.
