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
| `--tls-cert` | none | PEM certificate (or chain); enables HTTPS/WSS. |
| `--tls-key` | none | PEM private key for `--tls-cert`. |
| `--token` | `$WEBTERM_TOKEN`, else none | Require HTTP Basic auth with this shared secret. |
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

`build_ssl_context(certfile, keyfile)` constructs the server `SSLContext`
(TLS 1.2 minimum) from the `--tls-cert`/`--tls-key` files and passes it to
`serve()`. Bad or missing PEM files are reported through `parser.error`.

When `serve()` returns `True` (a client asked to restart, or the process got
`SIGHUP`), `main()` re-execs the interpreter with `python -m webterm` and the
original arguments. Using `-m webterm` rather than `sys.argv[0]` keeps working
whether webterm was started from the console script or as a module.

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

When a TLS context is supplied, `serve` passes it as `ssl=` to
`asyncio.start_server`, so HTTP and the WebSocket upgrade share one TLS
listener; `handle_client` is unaware of the transport. The startup log line
switches from `http://` to `https://` accordingly.

`check_auth(headers, token)` gates every request when a token is configured.
It accepts HTTP Basic credentials when either the username or the password
equals the secret and does a constant-time comparison; otherwise
`handle_client` answers `401 Unauthorized` with a `WWW-Authenticate: Basic`
challenge before routing anything.

`handle_client` ties it together: parse the request, enforce auth, route a
WebSocket upgrade on `/ws` (path exact match) to `WebSocket.handshake`, route
`/config.js` to `serve_config`, and otherwise fall back to static serving. The
connection is always closed in `finally`.

### `ClientConnection`

One instance per browser WebSocket. It bridges the manager's synchronous
fan-out to the async socket:

- `offer_output(session, data, offset)` / `offer_exit(session)` are called from
  the event loop by `Session`. They push onto `out_queue`. If the queue is full
  the connection is flagged for closure (backpressure) rather than dropping
  output.
- `_send_loop` dequeues and sends. `None` payloads become
  `{"type":"exit","session_id":...}`; byte payloads become binary frames
  `bytes([len(sid)]) + sid + struct.pack(">Q", offset) + payload`.
- `_read_loop` decodes JSON text frames and dispatches `_handle`. Malformed
  JSON is ignored; per-message exceptions are logged without killing the
  connection.
- `run()` starts sender, reader, and a close-event waiter, and on exit detaches
  from every session and closes the socket.

### Command messages (browser → server)

| Type | Fields | Action |
|---|---|---|
| `subscribe` | `session_id`, `since?` | Attach and send retained output from the absolute byte `since` (default 0) plus `attached`. Unknown id triggers a fresh `sessions` list. |
| `unsubscribe` | `session_id` | Detach. |
| `create` | `cols`, `rows`, `title?`, `command?` | Spawn a session. `command` may be a non-empty string (a shell command line) or a non-empty list of strings (argv). |
| `kill` | `session_id` | Terminate the session. |
| `rename` | `session_id`, `title` | Set the title (capped at 200 chars) and broadcast. |
| `input` | `session_id`, `data` | Write UTF-8 bytes to the PTY. |
| `resize` | `session_id`, `cols`, `rows` | `TIOCSWINSZ` on the PTY. |
| `list` | | Re-send the session list. |
| `restart` | | Ask the process to re-exec itself. Broadcasts `restarting`, then shuts down. |

Unknown types are logged at debug level. Binary frames from the client are
ignored.

### Server messages (server → browser)

| Type | Fields | Meaning |
|---|---|---|
| `sessions` | `sessions` | Full list of session info dicts. Sent on connect, on `list`, and whenever the manager changes. |
| `attached` | `session` | Acknowledge a subscribe, with that session's info. |
| `exit` | `session_id` | The child process exited. |
| `restarting` | | The server is about to re-exec; reconnect once the socket drops. |

Binary frames carry terminal output as described in
[architecture.md](architecture.md#wire-protocol).

### Server lifecycle

`serve()` installs a manager listener that pushes updated session lists to all
connections, starts the TCP server, and installs signal handlers. A shared
`ServerControl` carries process-lifecycle requests: `SIGINT`/`SIGTERM` request a
plain stop, `SIGHUP` and the `restart` command request a restart. Both set the
same shutdown event; `restart_requested` distinguishes them.

Shutdown stops accepting, asks every connection to close, closes all sessions,
cancels client tasks, and waits (bounded) for `server.wait_closed()`. If a
restart was requested, it first broadcasts `restarting` to every connection, and
`serve()` returns `True` so `__main__.main()` can re-exec. The comment in the
code explains why `wait_closed()` is bounded: from Python 3.12 it waits for
active connections, so an open browser WebSocket would otherwise hang Ctrl+C.

## `session.py` — PTYs, scrollback, and reaping

### `Session`

Owns one PTY master fd and its child process.

- Construction sets the fd non-blocking, registers `loop.add_reader(fd,
  _on_readable)`, and applies the initial window size.
- `info()` is the dict serialized to the browser: `id`, `title`, `cols`,
  `rows`, `pid`, `alive`, `exit_code`.
- `_on_readable` loops `os.read` until it would block, records the chunk's
  absolute offset, appends to scrollback, and offers the bytes to all
  subscribers.
- `_append` maintains the bounded ring buffer with `--scrollback` **bytes**.
  When it overflows it trims to the next newline so replay starts on a line
  boundary. `total` tracks every byte ever emitted and `buffer_start` is the
  absolute offset of the oldest retained byte.
- `replay_since(offset)` returns `(start, data)` for a client that has consumed
  `offset` bytes: `start = max(offset, buffer_start)`, and `data` is the
  retained tail from there (empty when the client is caught up).
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
- If a `command` is supplied, it is typed into the shell after a 0.2 s delay,
  so it behaves exactly like user input and control returns to the prompt
  afterward. A list is `shlex.join`ed first, while a string is sent verbatim
  (preserving `&&`, pipes and globs).
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
