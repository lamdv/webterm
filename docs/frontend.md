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
| `webterm/static/pi.js` | Structured pi chat view and event reducer, exposed as `window.WebtermPi`. |
| `/config.js` (generated) | Runtime config, served by the server, assigns `window.WEBTERM_CONFIG`. |

### Load order

`index.html` loads, in order:

1. `xterm.css` from jsDelivr (pinned, `integrity`, `crossorigin`).
2. `/style.css`.
3. `xterm.js` from jsDelivr (pinned, `integrity`, `crossorigin`).
4. `/config.js` (generated).
5. `/highlight.js`.
6. `/pi.js`.
7. `/app.js`.

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
  `launchers`, and `pi` (`available`, `cwd`, `sessionDir`).
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
   to the newest; otherwise keep or fix `activeId`.

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
  (`session.agent`, else `pi` for `kind:"pi"`, else `agent`) render it as a
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

### Pi view

`ensurePiView` creates `window.WebtermPi.create(session, hooks)` for
`kind:"pi"` sessions. The view is hydrated by server `pi_resync` snapshots and
then consumes `pi_event` records; it owns no persistence. Hooks map to the
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
`/tree`, `/export`) and merges pi's `get_commands` results for
extension/template/skill commands. `/model` and `/provider` are webterm-side
pickers over `get_available_models`; selecting an entry sends `set_model` and
updates the bottom status bar's model label from the response without a full
resync.

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
focus. Interactive elements also get `touch-action: manipulation` to drop the
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
relabels the submit button, and runs `createPiSession()` (the trust dialog) on
submit.

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

All storage access is wrapped in `try/catch`, since storage may be unavailable
(private mode, etc.).

## Testing and linting

There is no browser test runner. The server's tests assert that `index.html` is
served and that the xterm.js CDN tag is present (`tests/test_server.py`). The
frontend is otherwise exercised manually. Keep `app.js` dependency-free and avoid
introducing a build step; anything added must run from the served static files.
