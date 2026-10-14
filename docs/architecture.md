# Architecture

High-level overview of webterm: how the pieces fit together, how data flows, and
which invariants the code relies on. For module-level detail see
[backend.md](backend.md) and [frontend.md](frontend.md).

## What webterm is

A web-based terminal multiplexer. A single Python process owns persistent PTY
sessions (like `tmux`/`screen`); browser tabs are thin clients that attach over a
WebSocket. Because sessions live in the server process, closing the browser,
refreshing, or moving to another machine does not disturb them, and scrollback is
replayed on reattach.

## Design principles

- **Standard library only.** No third-party Python runtime dependencies. The
  WebSocket protocol, HTTP handling, static serving, and PTY management are all
  hand-rolled on `asyncio`.
- **No frontend build step.** Static HTML/CSS/JS served directly; xterm.js is
  loaded from a pinned, integrity-checked CDN URL.
- **POSIX only.** Session creation uses `pty.fork()`; Windows is unsupported.
- **Server is the source of truth.** Session identity, lifetime, size, and
  scrollback belong to the server. The browser is stateless and disposable.
- **One event loop.** All I/O is non-blocking; there are no threads and no
  blocking calls in the loop.

## Components

```
┌──────────────────────────── browser ────────────────────────────┐
│  index.html                                                      │
│    ├─ xterm.js (CDN)                                             │
│    ├─ config.js  (runtime config injected by the server)         │
│    └─ app.js     (terminal registry, tabs, launchers, socket)    │
│  one xterm.js instance per attached session                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │ WebSocket /ws
                                │  text frames = JSON control
                                │  binary frames = terminal bytes
┌───────────────────────────────▼─────────────────────────────────┐
│                       asyncio server (server.py)                 │
│  HTTP: static files, /config.js, WebSocket upgrade               │
│  ClientConnection: per-browser fan-out + command dispatch        │
│                                                                  │
│  SessionManager (session.py)                                     │
│    ├─ Session  ── PTY master fd ── shell / child process         │
│    │    ├─ bounded scrollback ring buffer                        │
│    │    └─ subscriber set (ClientConnections, possibly many)     │
│    └─ SIGCHLD reaper: waits children, marks sessions dead        │
│                                                                  │
│  WebSocket (websocket.py): RFC 6455 subset, framing, handshake   │
└──────────────────────────────────────────────────────────────────┘
```

## Process and connection model

- One TCP listener (`asyncio.start_server`). Each accepted connection runs
  `handle_client` as a task. A connection is either a static-file request
  (`GET`), the generated `/config.js`, or a WebSocket upgrade on `/ws`.
- A single browser WebSocket can **multiplex every session**: it subscribes to
  sessions and receives all their output, demultiplexed by an embedded
  session-id prefix (see below).
- Multiple browsers may attach to the same session at once; each has its own
  `ClientConnection` and receives its own copy of the fan-out.
- A session can outlive all browsers. Output produced while nobody is attached
  still goes into the scrollback buffer, which is replayed on the next attach.

## Data flow

### Spawning a session

1. Browser sends `{"type":"create", ...}` (optionally with a `command` argv).
2. `ClientConnection._create` validates and calls `SessionManager.create`.
3. `SessionManager` does `pty.fork()`. The child resets signals, sets `TERM`,
   `chdir`s to the configured cwd, and `exec`s the shell.
4. The parent wraps the PTY master fd in a `Session`, registers an
   `add_reader` callback, assigns an id, and notifies listeners.
5. The notification pushes an updated session list to every connection. If a
   command was supplied, it is typed into the interactive shell shortly after so
   the user lands at a prompt when it exits.

### Input

Browser keystrokes → `{"type":"input","session_id":...,"data":...}` →
`Session.write` → buffered non-blocking write to the PTY master. If the PTY
would block, an `add_writer` callback drains the remaining buffer later.

### Output

The event loop invokes `Session._on_readable`, which reads up to `READ_SIZE`
bytes, appends to the scrollback ring buffer, and offers the bytes to every
attached subscriber. `ClientConnection._offer` enqueues `(session_id, bytes)`
on a bounded queue; a sender task encodes each item as a binary frame with a
one-byte session-id length prefix and writes it to the socket.

### Attach / replay

On subscribe, a connection is attached to the session and the current scrollback
snapshot is enqueued **without awaiting** between attach and enqueue, so live
output cannot slip ahead of replayed history.

### Exit

When a child exits, `SIGCHLD` reaping calls `Session._on_child_exit`, which
marks the session dead, removes its loop readers, closes the fd, notifies
subscribers (an `exit` control message), and removes it from the manager. The
manager then broadcasts the updated session list.

## Wire protocol

One WebSocket per browser, carrying two frame types:

- **Text frames** — JSON control messages. Browser → server: `subscribe`,
  `unsubscribe`, `create`, `kill`, `rename`, `input`, `resize`, `list`.
  Server → browser: `sessions`, `attached`, `exit`.
- **Binary frames** — terminal output. Layout:
  `[1 byte id length][id bytes (UTF-8)][raw terminal output]`.
  The per-frame session id lets one socket carry many sessions.

Terminal bytes are never interpreted by the server (except that future features
may parse escape sequences for metadata such as titles); they are passed through
verbatim to xterm.js.

See [backend.md](backend.md#protocol) for message-by-message detail and
[frontend.md](frontend.md#websocket-protocol-handling) for the client side.

## Concurrency and backpressure

- All work runs on one event loop. PTY reads use `loop.add_reader`; PTY writes
  use `loop.add_writer` only when the socket/Pty would block.
- Each connection has a bounded output queue (2048 items). If a client cannot
  keep up and the queue fills, the connection is **closed** rather than silently
  dropping terminal bytes, so the client can reconnect and replay history.
- Per-connection writes are serialized with a lock inside `WebSocket`.
- Child processes are reaped by a single `SIGCHLD` handler in `SessionManager`
  (asyncio no longer provides child watchers). Reaping the child in the same
  process avoids races between `waitpid` and session teardown.

## State and persistence

All state is in memory in the server process. There is no database or session
file. `--scrollback` bounds the per-session replay buffer; the manager removes
dead sessions from its map. Restarting the server destroys all sessions and
history. Launcher definitions and UI preferences that survive reload live in the
browser's `localStorage`, not on the server.

## Configuration

Runtime configuration flows to the browser through a generated `/config.js`
script that assigns `window.WEBTERM_CONFIG`. The CLI (`webterm/__main__.py`)
builds this dict from flags (`--font`, `--launcher`) and supplies it to
`serve`. This avoids templating `index.html` and keeps the static assets
cacheable.

## Security model

- Default bind is `127.0.0.1`. Anyone who can reach the server gets a shell;
  there is no authentication.
- Exposing it on `0.0.0.0` requires understanding that risk; the README warns
  about it and recommends a reverse proxy for auth/TLS.
- Static serving resolves paths under `static/` and rejects traversal.

## Extensibility points

- **Launchers** — `--launcher LABEL=COMMAND` adds sidebar buttons; the default
  is a `pi agent` button when `pi` is on `PATH`.
- **Runtime config** — anything added to the config dict is available to
  `app.js` as `window.WEBTERM_CONFIG`.
- **Protocol** — new control message types can be added without breaking
  existing clients; the binary output framing is the stable core.
- Candidate agent-oriented work is described in [pi-integration.md](pi-integration.md).

## Testing architecture

Tests use `unittest` (not pytest, despite the `[tool.pytest.ini_options]`
section). `tests/wsclient.py` is a stdlib masked WebSocket client, so the
protocol functions (`server.py`, `websocket.py`, `session.py`) are exercised
end-to-end without any dependency. See [backend.md](backend.md#testing) for
details.
