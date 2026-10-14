# Backend

The Python side of webterm: a dependency-free `asyncio` server that owns PTY
sessions and speaks a small WebSocket protocol. This document covers every
module, the message contract, and the lifecycle details that matter when
changing the code. For the big picture see [architecture.md](architecture.md);
for the client see [frontend.md](frontend.md).

## Modules at a glance

| Module | Responsibility |
|---|---|
| `webterm/__main__.py` | CLI entry point: argument parsing, config assembly, `asyncio.run`. |
| `webterm/server.py` | HTTP static serving, WebSocket upgrade, `ClientConnection` command loop and fan-out, shutdown. |
| `webterm/session.py` | `Session` (PTY + scrollback + subscribers) and `SessionManager` (spawn, reap, list, kill). |
| `webterm/websocket.py` | Minimal RFC 6455 server: handshake, framing, fragmentation, ping/pong/close. |
| `webterm/__init__.py` | Package version. |

All modules use `from __future__ import annotations` and are linted by `ruff`
(line length 100, rules `E, F, I, UP, B`).

## `__main__.py` — CLI and configuration

`build_parser()` defines the flags:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Bind interface. Do not change the default without a reason. |
| `--port` | `8765` | Bind port. |
| `--shell` | `$SHELL` or a detected shell | Shell to launch. |
| `--font` | Nerd Font aware stack | CSS `font-family` passed to the client. |
| `--launcher LABEL=COMMAND` | `pi agent` if `pi` is installed | Sidebar launcher button (repeatable). |
| `--cwd` | `$HOME` | Working directory for new sessions. |
| `--scrollback` | `100000` | Bytes retained per session for replay. |
| `--verbose`, `-v` | off | Debug logging. |
| `--version` | | Print version. |

`main()` resolves the shell (`_default_shell` tries `$SHELL`, `/bin/bash`,
`/bin/zsh`, `/bin/sh`), constructs a `SessionManager`, builds the client config
dict, and runs `serve()`.

`parse_launchers` turns `LABEL=COMMAND` into `{"label": ..., "command": [...]}`
using `shlex.split`. When no `--launcher` is given, it probes for `pi` with
`shutil.which` and adds a default button.

The config dict currently supports `fontFamily` and `launchers`; it is served as
`window.WEBTERM_CONFIG` by `/config.js`.

## `websocket.py` — the WebSocket layer

`WebSocket` is the server side of one connection. It implements only what
webterm needs:

- `handshake(reader, writer, headers)` verifies `Sec-WebSocket-Key` and writes
  the `101 Switching Protocols` response. It is called with an
  already-parsed request, so `server.py` owns HTTP parsing.
- `recv()` returns the next complete data message as `(opcode, payload)`, or
  `None` on close. It transparently handles control frames: `PING` is answered
  with `PONG`, `PONG` is ignored, `CLOSE` is echoed and ends the stream.
- Fragmentation is reassembled (`OP_CONT`), with a guard against a new data
  frame starting before the previous one finishes.
- Client frames must be masked; the server never masks its frames.
- `DEFAULT_MAX_MESSAGE` is 8 MiB, enforced both while reading a frame and while
  reassembling.
- `send_text` / `send_binary` / `send_json` / `send_ping` / `close` serialize
  writes with an internal `asyncio.Lock` and translate transport errors into
  `ConnectionClosed`.
- `accept_key` implements the `SHA1(key + GUID)` + base64 handshake digest.

Exceptions: `WebSocketError` for protocol violations (the connection should be
dropped), `ConnectionClosed` when the peer disappears.

## `server.py` — HTTP, connections, and command dispatch

### HTTP request handling

- `read_http_request(reader)` reads until `\r\n\r\n` (30 s timeout, 64 KiB
  header cap), returns `(method, path, headers)`, and strips the query string.
- `resolve_static(path)` maps `/` to `index.html`, resolves the candidate under
  `STATIC_ROOT`, and returns `None` on traversal or a missing file.
- `serve_static` answers `GET`/`HEAD` with the right content type from
  `CONTENT_TYPES`; anything else is `405`.
- `serve_config` generates `/config.js` as
  `window.WEBTERM_CONFIG = {...};` with `Cache-Control: no-store`.

`handle_client` ties it together: parse the request, route a WebSocket upgrade
on `/ws` (path exact match) to `WebSocket.handshake`, route `/config.js` to
`serve_config`, and otherwise fall back to static serving. The connection is
always closed in `finally`.

### `ClientConnection`

One instance per browser WebSocket. It bridges the manager's synchronous
fan-out to the async socket:

- `offer_output(session, data)` / `offer_exit(session)` are called from the
  event loop by `Session`. They push onto `out_queue`. If the queue is full the
  connection is flagged for closure (backpressure) rather than dropping output.
- `_send_loop` dequeues and sends. `None` payloads become
  `{"type":"exit","session_id":...}`; byte payloads become binary frames
  prefixed with `bytes([len(sid)]) + sid`.
- `_read_loop` decodes JSON text frames and dispatches `_handle`. Malformed
  JSON is ignored; per-message exceptions are logged without killing the
  connection.
- `run()` starts sender, reader, and a close-event waiter, and on exit detaches
  from every session and closes the socket.

### Command messages (browser → server)

| Type | Fields | Action |
|---|---|---|
| `subscribe` | `session_id` | Attach and send replayed scrollback + `attached`. Unknown id triggers a fresh `sessions` list. |
| `unsubscribe` | `session_id` | Detach. |
| `create` | `cols`, `rows`, `title?`, `command?` | Spawn a session. `command` must be a non-empty list of strings. |
| `kill` | `session_id` | Terminate the session. |
| `rename` | `session_id`, `title` | Set the title (capped at 200 chars) and broadcast. |
| `input` | `session_id`, `data` | Write UTF-8 bytes to the PTY. |
| `resize` | `session_id`, `cols`, `rows` | `TIOCSWINSZ` on the PTY. |
| `list` | | Re-send the session list. |

Unknown types are logged at debug level. Binary frames from the client are
ignored.

### Server messages (server → browser)

| Type | Fields | Meaning |
|---|---|---|
| `sessions` | `sessions` | Full list of session info dicts. Sent on connect, on `list`, and whenever the manager changes. |
| `attached` | `session` | Acknowledge a subscribe, with that session's info. |
| `exit` | `session_id` | The child process exited. |

Binary frames carry terminal output as described in
[architecture.md](architecture.md#wire-protocol).

### Server lifecycle

`serve()` installs a manager listener that pushes updated session lists to all
connections, starts the TCP server, and installs `SIGINT`/`SIGTERM` handlers.
Shutdown stops accepting, asks every connection to close, closes all sessions,
cancels client tasks, and waits (bounded) for `server.wait_closed()`. The
comment in the code explains why: from Python 3.12, `wait_closed()` waits for
active connections, so an open browser WebSocket would otherwise hang Ctrl+C.

## `session.py` — PTYs, scrollback, and reaping

### `Session`

Owns one PTY master fd and its child process.

- Construction sets the fd non-blocking, registers `loop.add_reader(fd,
  _on_readable)`, and applies the initial window size.
- `info()` is the dict serialized to the browser: `id`, `title`, `cols`,
  `rows`, `pid`, `alive`, `exit_code`.
- `_on_readable` loops `os.read` until it would block, appends to scrollback,
  and offers bytes to all subscribers.
- `_append` maintains the bounded ring buffer with `--scrollback` **bytes**.
  When it overflows it trims to the next newline so replay starts on a line
  boundary.
- `write` buffers into `_out` and calls `_flush`, which uses `add_writer` if the
  PTY is momentarily full.
- `resize` clamps to at least 1×1 and issues `TIOCSWINSZ`.
- `_mark_dead` is idempotent: removes readers/writers, closes the fd, notifies
  subscribers with `offer_exit`, and invokes the manager's `on_exit`.
- `close()` sends `SIGTERM` then marks dead.

### `SessionManager`

- `create()` calls `pty.fork()`. In the child, `_exec_child` clears the signal
  mask, resets signal dispositions (so Node/TUI children behave), sets `TERM`
  and `COLORTERM`, `chdir`s to `cwd`, and `execvpe`s the shell.
- Session ids are `"{counter}-{pid}"`. Treat them as opaque.
- If a `command` is supplied, it is `shlex.join`ed and typed into the shell
  after a 0.2 s delay, so it behaves exactly like user input and control returns
  to the prompt afterward.
- `_install_reaper` registers a `SIGCHLD` handler once. `_reap_children` loops
  `os.waitpid(-1, WNOHANG)`, maps pid → session via `_by_pid`, and reports the
  exit code.
- `_on_session_exit` removes the session and calls `notify()`.
- `add_listener` / `notify` implement the broadcast mechanism used by
  `server.py`.
- `kill` and `close_all` terminate sessions.

`DEFAULT_SCROLLBACK` is 100 000 bytes; `READ_SIZE` is 65 536.

## Protocol

See [architecture.md](architecture.md#wire-protocol) for the framing overview
and the tables above for the exact message shapes. The stable invariants are:

- Text frames are always JSON objects with a `type` field.
- Binary frames are always `[len(id)][id][payload]`.
- Session ids are opaque strings chosen by the server.

## Testing

Run the suite with:

```bash
uv sync --extra dev
uv run ruff check .
uv run python -m unittest discover -s tests -t . -v
```

- `tests/wsclient.py` — a stdlib masked WebSocket client (`WSClient`) with
  helpers to send JSON/binary and receive frames/messages.
- `tests/test_websocket.py` — framing and handshake unit tests.
- `tests/test_server.py` — end-to-end static serving and session protocol tests
  using a real `asyncio.start_server`.
- `tests/test_cli.py` — argument parsing and launcher config.

Tests use `unittest.IsolatedAsyncioTestCase`. Note that `pyproject.toml` has a
`[tool.pytest.ini_options]` section, but the suite is `unittest`-based.

## Style conventions

- Stdlib only; do not add runtime dependencies without an explicit request.
- Line length 100; imports sorted (`ruff check .` enforces `I`).
- Docstrings on modules, classes, and public functions.
- Keep I/O async or hand it to an executor; never block the event loop.
