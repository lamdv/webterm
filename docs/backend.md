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
| `webterm/session.py` | `Session` (PTY + scrollback + subscribers + lineage) and `SessionManager` (spawn, reap, list, kill, limits, GC). |
| `webterm/agent/session.py` | `AgentSession`: a canonical JSONL agent child over three pipes, command correlation, event replay, dialogs, and two-phase close. |
| `webterm/agent/codec.py` | The adapter seam: `AgentCodec` plus `NativeCodec`, which serves an `AgentSession` whose child already speaks canonical JSONL. |
| `webterm/agent/protocol.py` | Canonical agent protocol: capabilities, command names, hello validation, event names, response records, and payload validation. |
| `webterm/agent/process.py` | Fork/exec into stdin/stdout/stderr pipes with the shared `WEBTERM_*` child environment. |
| `webterm/integration/pi/session.py` | `PiSession`: a `pi --mode rpc` child over three pipes, JSONL event fan-out, snapshot/resync, server-owned extension dialogs/UI state, and two-phase close. |
| `webterm/integration/pi/canonical.py` | Translation from pi RPC events, snapshots, history, models, and extension UI into the canonical agent protocol. |
| `webterm/integration/pi/codec.py` | `PiCodec`: the canonical surface over a `PiSession`, so the server never branches on pi. |
| `webterm/integration/pi/models.py` | Model probe for the new-session picker: throwaway RPC child, field whitelist, per-cwd cache. |
| `webterm/integration/pi/storage.py` | pi session-file root, header parsing, path containment, and picker listing. |
| `webterm/integration/pi/config.py` | pi CLI defaults, availability/detection, frontend config, agent profile, and launcher. |
| `webterm/websocket.py` | Minimal RFC 6455 server: handshake, framing, fragmentation, ping/pong/close. |
| `webterm/transport.py` | `Transport` protocol and `FramedTransport` (length-prefixed frames for the control socket). |
| `webterm/control.py` | Local `AF_UNIX` control listener, discovery file, peer-credential check. |
| `webterm/ansi.py` | Best-effort ANSI/control-sequence stripping for `capture` / `wait --text`. |
| `webterm/client.py` | Blocking `ControlClient` for the local control socket. |
| `webterm/ctl.py` | The `webtermctl` command-line client. |
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
| `--token` | `$WEBTERM_TOKEN`, else none | Require this shared secret, via HTTP Basic, Bearer, or the bootstrap cookie. |
| `--shell` | `$SHELL` or a detected shell | Shell to launch. |
| `--font` | Nerd Font aware stack | CSS `font-family` passed to the client. |
| `--launcher LABEL=COMMAND` | `pi agent` if `pi` is installed | Sidebar launcher button (repeatable). |
| `--cwd` | `$HOME` | Working directory for new sessions. |
| `--scrollback` | `100000` | Bytes retained per session for replay. |
| `--pi-session-dir` | `$WEBTERM_PI_SESSION_DIR` or `$XDG_STATE_HOME/webterm/pi-sessions` | Managed root for persisted pi sessions, exports, and the resume picker. |
| `--llama-url` | `$WEBTERM_LLAMA_URL` or `$LLAMA_SWAP_URL` | llama-server/llama-swap base URL; adds a `llama` chat agent profile (see [llama.md](llama.md)). |
| `--llama-model` | `$WEBTERM_LLAMA_MODEL` | Default model for the `llama` profile. |
| `--llama-system-prompt` | `$WEBTERM_LLAMA_SYSTEM_PROMPT` | Initial system prompt for `llama` chats. |
| `--llama-thinking` | `$WEBTERM_LLAMA_THINKING` | Initial thinking level for `llama` chats (`default`, `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`). |
| `--control-socket` | `$XDG_RUNTIME_DIR/webterm/control.sock` | Unix socket for the local agent control API. |
| `--no-control` | off | Disable the local control socket. |
| `--agent NAME=COMMAND` | `pi` if installed | Agent profile for `spawn` (print mode); `{task}` is substituted. |
| `--agent-interactive NAME=COMMAND` | same as `--agent` | Interactive command for an agent profile. |
| `--agent-rpc NAME=COMMAND` | pi when installed | Structured RPC command for an agent profile. |
| `--max-agent-depth` | `5` | Maximum sub-agent nesting depth. |
| `--max-agent-children` | `16` | Maximum live child sessions per parent. |
| `--max-sessions` | `64` | Maximum concurrent sessions. |
| `--max-retained` | `32` | Maximum finished one-shot sessions retained. |
| `--retained-ttl` | `600` | Seconds a finished one-shot session is kept (negative disables). |
| `--verbose`, `-v` | off | Debug logging. |
| `--version` | | Print version. |

`main()` resolves the shell (`_default_shell` tries `$SHELL`, `/bin/bash`,
`/bin/zsh`, `/bin/sh`), constructs a `SessionManager`, builds the client config
dict, and runs `serve()`.

`parse_launchers` turns `LABEL=COMMAND` into `{"label": ..., "command": [...]}`
using `shlex.split`. When no `--launcher` is given, it probes for `pi` with
`shutil.which` and adds a default button only when pi is installed. Agent
profiles carry `print`, `interactive`, and optional `rpc` templates plus an
optional `adapter` name. The default `pi` RPC profile and every browser-created
pi session load the bundled `webterm/integration/pi/extension.ts` extension;
the `llama` profile starts the canonical adapter described in
[llama.md](llama.md), forwarding the configured model, system prompt, and
thinking level as adapter flags.

The config dict currently supports `fontFamily`, `launchers`, `agents`, and
`pi` (`available`, `cwd`, `sessionDir`, `version`, `testedVersion`); it is
served as
`window.WEBTERM_CONFIG` by `/config.js`. At startup `main()` creates and
resolves the pi session root (`0700`) only when pi is installed or
`--pi-session-dir` was given explicitly. Without pi, `pi_available` is false,
`sessionDir` is `null`, pi launchers/profiles are omitted, and a `create`
request for a pi session is rejected with an `unavailable` error instead of
spawning a doomed child.

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
It accepts HTTP Basic credentials (either the username or the password may
carry the secret), `Authorization: Bearer <token>`, or the `webterm_session`
cookie set by the bootstrap flow, and compares every candidate in constant
time. When none match, `handle_client` answers `401 Unauthorized` with a
`WWW-Authenticate: Basic` challenge before routing anything.

`serve_discovery` answers `GET`/`HEAD /.well-known/webterm.json` with a small
unauthenticated document: the service name, the version, the wire-protocol
version, whether a token is required, and which credential types are accepted.
It is served *before* `check_auth`, so a client can classify a server before it
holds credentials, and `--no-discovery` removes it.

`handle_client` ties it together: parse the request, serve discovery, handle a
`?bootstrap=` exchange, enforce auth, route a WebSocket upgrade on `/ws` (path
exact match) to `WebSocket.handshake`, route `/config.js` to `serve_config`, and
otherwise fall back to static serving. The connection is always closed in
`finally`.

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
| `create` | `cols`, `rows`, `title?`, `command?`, `mode?`, `cwd?`, `trust?`, `provider?`, `model?`, `resume?`, `env?` | Spawn a session. `command` may be a non-empty string (a shell command line) or a non-empty list of strings (argv). With `mode: "pi"` the rest of the pi fields apply: `provider`/`model` select the model (`--model` alone is enough; `provider` without `model` is rejected), and a missing `model` falls back to `--default-pi-model` unless `resume` names a session file. |
| `pi_models` | `cwd?`, `refresh?` | Models pi offers for `cwd` (default: the manager's cwd), from the cached probe. Answers `{models, default, fetchedAt, cwd, cached}`; `default` is the model a session would start with. Needs no session, so the picker works before one exists. |
| `kill` | `session_id` | Terminate the session. |
| `rename` | `session_id`, `title` | Set the title (capped at 200 chars) and broadcast. |
| `input` | `session_id`, `data` | Write UTF-8 bytes to the PTY. |
| `resize` | `session_id`, `cols`, `rows` | `TIOCSWINSZ` on the PTY. |
| `list` | `parent_id?` | Re-send the session list (and return it when `id` is set). |
| `run` | `command`, `cwd?`, `env?`, `parent_id?`, `timeout?`, `wait?`, `format?`, `strip_ansi?` | Run a command in its own PTY; return output + exit code, or a session id when `wait` is false. |
| `spawn` | `agent?`, `task?`, `command?`, `mode?`, `parent_id?`, `wait?` | Start a sub-agent from an agent profile; otherwise like `run`. A `task` is appended to an explicit `command`, or sent as the first `prompt` when `rpc` is set. |
| `send` | `session_id`, `data`, `submit?` | Write to a session (`submit` appends a newline). |
| `capture` | `session_id`, `since?`, `format?`, `strip_ansi?` | Read scrollback from an absolute byte offset. |
| `wait` | `session_id`, `until` (`exit`\|`text`\|`idle`), `pattern?`, `timeout?`, `quiet_ms?` | Block until exit, a regex matches, or output goes quiet. |
| `close` | `session_id` | Drop a finished retained session. |
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
| `result` | `id`, `result` | Correlated reply to a request that carried an `id`. |
| `error` | `id`, `error.{code,message}` | Correlated failure (`bad_request`, `not_found`, `timeout`, `depth_exceeded`, `too_many_sessions`, `conflict`, `internal`). |

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

### Local control plane (`control.py`, `transport.py`)

`ClientConnection` is transport-agnostic: it talks to a `Transport` protocol
(`send_json`, `send_binary`, `recv`, `close`). The browser passes a `WebSocket`;
the local control socket passes a `FramedTransport`, whose frames are
`u32 big-endian length | u8 kind | payload` with kind 1 = JSON text and kind 2
the same session-id/offset binary output frame the browser receives. Requests
may carry an `id` and then get a correlated `result`/`error`; browser requests
omit it. This is what lets one dispatcher serve the browser and `webtermctl`.

`ControlServer` binds the Unix socket, writes
`$XDG_RUNTIME_DIR/webterm/server.json` (`{socket, url, pid}`), and checks
`SO_PEERCRED` on Linux (uid must equal `os.geteuid()`; elsewhere the `0600`
mode is the boundary). It is an `AF_UNIX` socket only — `start_server` rejects a
`--control-socket` value that looks like a TCP address via `looks_like_tcp` — so
network reachability of the UI never grants programmatic control. `webtermctl`
(`ctl.py`) and `webterm.client.ControlClient` are the stdlib clients; both
autofill `parent_id` from `$WEBTERM_SESSION_ID` so a nested agent spawns visible
children with one command. `session.set_control_env()` injects `WEBTERM_SOCKET`,
`WEBTERM_URL`, `WEBTERM_SESSION_ID`, `WEBTERM_PARENT_SESSION`, `WEBTERM_DEPTH`,
and `WEBTERM_AGENT` into every child before `exec`.

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
  `server.py`. A canonical `AgentSession` calls `_changed()` only when
  `info()` would differ — run state, usage, or model — so streaming deltas do
  not rebroadcast the whole session list once per token.
- `kill` and `close_all` terminate sessions.

`DEFAULT_SCROLLBACK` is 100 000 bytes; `READ_SIZE` is 65 536.

## Protocol

See [architecture.md](architecture.md#wire-protocol) for the framing overview
and the tables above for the exact message shapes. The stable invariants are:

- Text frames are always JSON objects with a `type` field.
- Binary frames are always `[len(id)][id][payload]`.
- Session ids are opaque strings chosen by the server.
- A connection can opt into the canonical agent shadow stream with
  `{"type":"agent_api","enabled":true}` or `"agent_api":true` on `subscribe`;
  the legacy `pi_*` messages are unchanged either way.
- Canonical commands travel in
  `{"type":"agent","session_id":...,"command":{...}}` and answer with a
  canonical `response` record in `agent_response`. See
  [agent-protocol.md](agent-protocol.md#browser-envelope).
- `create` accepts `mode: "agent"` with `command` (argv or shell string),
  `adapter`, and an optional first `prompt`; `spawn` accepts `rpc: true` plus
  `adapter` (default `"pi"`) so a native agent can be a sub-agent too.
- Structured sessions report `kind: "agent"` and an `adapter`; only pi also
  carries the legacy `pi_*` messages, gated on that adapter rather than the
  kind.

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
