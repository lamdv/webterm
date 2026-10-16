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
- In addition to the TCP listener, the server binds a **local Unix control
  socket** for agents and scripts (see below). It is `AF_UNIX` only and never
  affects, or is affected by, `--host`.

## Sub-agents and the local control plane

Sessions carry lineage: `parent_id`, `depth`, `role` (`shell`/`agent`),
`agent`, `task`, `origin`, and `visibility`. The browser renders the session
list as a tree by `parent_id`, so a sub-agent nests under the session that
spawned it, with a badge and exit status, and the human can click it and type.

An agent running inside a session inherits `WEBTERM_SOCKET` and
`WEBTERM_SESSION_ID` (plus `WEBTERM_PARENT_SESSION`, `WEBTERM_DEPTH`,
`WEBTERM_URL`, `WEBTERM_AGENT`). It delegates with `webtermctl spawn --agent
... --task ...`, which connects to the local control socket; `webtermctl` fills
`parent_id` from `WEBTERM_SESSION_ID`, so the child nests automatically — and
recursively, since the child inherits the same variables.

The control socket is a second, local-only transport for the same command
dispatcher as the browser WebSocket (see [backend.md](backend.md)). It is
`AF_UNIX` only, mode `0600` inside a `0700` directory, with a peer-credential
check; it is never on the network. Recursion is bounded by `--max-agent-depth`
and `--max-sessions`, `kill` cascades to descendants by default, and finished
one-shot sessions are swept by TTL/count. The browser transport is unchanged and
keeps its own remote-access story (`--token`, TLS, a reverse proxy).

## Data flow

### Spawning a session

1. Browser sends `{"type":"create", ...}` (optionally with a `command` argv
   list or a shell command string).
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
attached subscriber. Each offer carries the **absolute offset** of its first
byte (the count of PTY bytes emitted since the session was created).
`ClientConnection._offer` enqueues `(session_id, offset, bytes)` on a bounded
queue; a sender task writes each item as a binary frame with a one-byte
session-id length, the id, the 8-byte big-endian offset, and the payload.

### Attach / replay

A `subscribe` may carry `since`, the absolute offset the client has already
rendered. On subscribe, a connection is attached to the session and the
retained bytes at or after `since` are enqueued **without awaiting** between
attach and enqueue, so live output cannot slip ahead of replayed history. The
replay starts at `max(since, buffer_start)`, so a client whose offset fell out
of the retained window is replayed from the tail; the frame offset reveals the
gap. Reconnecting clients pass their consumed offset and receive only the
missing tail, which leaves terminal state (alternate buffer, mouse tracking,
scrollback) intact.

### Exit

When a child exits, `SIGCHLD` reaping calls `Session._on_child_exit`, which
marks the session dead, removes its loop readers, closes the fd, notifies
subscribers (an `exit` control message), and removes it from the manager. The
manager then broadcasts the updated session list.

## Wire protocol

One link carries two frame types — the browser WebSocket, and (for local
clients) the length-prefixed control socket:

- **Text frames** — JSON control messages. Browser → server: `subscribe`,
  `unsubscribe`, `create`, `kill`, `rename`, `input`, `resize`, `list`,
  `restart`. Server → browser: `sessions`, `attached`, `exit`, `restarting`.
- **Binary frames** — terminal output. Layout:
  `[1 byte id length][id bytes (UTF-8)][8-byte big-endian offset][terminal output]`.
  The per-frame session id lets one socket carry many sessions; the offset
  locates the output in the session's absolute byte stream so a reconnect can
  resume exactly (or detect that it fell behind the scrollback window).

A `restart` command (or `SIGHUP` sent to the server process) asks the process to
re-exec itself in place. Every session is closed, each browser is sent
`restarting` and disconnected, and the client reconnects to the fresh process.
Because the process image is replaced with `os.execv`, the PID and listening
port are preserved and no external supervisor is needed. This is deliberate: a
restart always drops all sessions and scrollback.

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
history — that is the point of the sidebar `⟳` button (or `SIGHUP`), which
re-execs the process so edited code takes effect; the browser reconnects to the
new process. Launcher definitions and UI preferences that survive reload live in
the browser's `localStorage`, not on the server.

## Configuration

Runtime configuration flows to the browser through a generated `/config.js`
script that assigns `window.WEBTERM_CONFIG`. The CLI (`webterm/__main__.py`)
builds this dict from flags (`--font`, `--launcher`) and supplies it to
`serve`. This avoids templating `index.html` and keeps the static assets
cacheable.

## Security model

- Default bind is `127.0.0.1`. Anyone who can reach the server gets a shell
  when no token is configured.
- `--token` (or `$WEBTERM_TOKEN`) enables a lightweight shared-secret guard:
  `check_auth()` runs before every HTTP request and WebSocket upgrade and
  accepts HTTP Basic, `Authorization: Bearer`, or the bootstrap cookie, so
  static files, `/config.js`, the WebSocket, and terminal I/O are all covered
  by one check. The secret is compared with `hmac.compare_digest`.
- `GET /.well-known/webterm.json` is the single unauthenticated route: it
  reveals only the service name, the version and the accepted credential types,
  so a client can classify a server before it holds any credential.
  `--no-discovery` removes it.
- `--tls-cert`/`--tls-key` wrap the single `asyncio.start_server` call in an
  `ssl.SSLContext`, so the same socket speaks HTTPS and WSS. This is what makes
  the token meaningful off localhost: TLS encrypts the secret and the terminal
  stream and authenticates the server, closing the man-in-the-middle gap that
  Basic-over-plain-HTTP cannot. The client already selects `wss:` from
  `location.protocol`, so no frontend change is needed.
- Exposing it on `0.0.0.0` requires understanding the risk; the README warns
  about it, and `__main__.py` logs a warning when binding a non-loopback host
  without TLS or without a token. Without TLS, Basic auth is only base64 and the
  token travels on every request.
- Static serving resolves paths under `static/` and rejects traversal.

## Extensibility points

- **Launchers** — `--launcher LABEL=COMMAND` adds sidebar buttons; the default
  is a `pi agent` button when `pi` is on `PATH`.
- **Runtime config** — anything added to the config dict is available to
  `app.js` as `window.WEBTERM_CONFIG`.
- **Protocol** — new control message types can be added without breaking
  existing clients; the binary output framing is the stable core.

## Testing architecture

Tests use `unittest` (not pytest, despite the `[tool.pytest.ini_options]`
section). `tests/wsclient.py` is a stdlib masked WebSocket client, so the
protocol functions (`server.py`, `websocket.py`, `session.py`) are exercised
end-to-end without any dependency. See [backend.md](backend.md#testing) for
details.
