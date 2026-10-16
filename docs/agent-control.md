# Proposal: sub-agents in webterm (local agent control API)

Status: **draft for discussion** · Owner: _unassigned_ · Depends on: `b962620`
(retained one-shot sessions)

## 1. The user story

> I run an agent (say `pi`) inside a webterm session. That agent decides it needs
> to delegate: it spawns a **sub-agent** for a specific task. I want to *see*
> that sub-agent working — live, in my sidebar — take it over if I want, and let
> the parent agent collect its result. In the future, webterm itself should
> offer a default agentic interface that spawns sub-agents for tasks.

So this is not primarily about scripts driving a terminal. It is about
**hierarchical agents** running on top of the same session pool the human is
already watching. The terminal is the substrate; the feature is orchestration
with visibility.

### 1.1 The loop we want to enable

```
human ──► session A: pi (interactive, human-visible)
                 │
                 │  "spawn a sub-agent to run the test suite"
                 ▼
          webterm control API  ──►  session B: pi -p "run the tests"   (child of A)
                 ▲                        │
                 └── result: exit 1, output ◄┘
```

1. Agent A is running inside a webterm PTY, so it inherits `WEBTERM_SOCKET` and
   `WEBTERM_SESSION_ID`.
2. A calls the local control API: *spawn a sub-agent with this task*.
3. webterm creates session B as a **child of A**, with the agent command, and
   broadcasts it, so the browser renders it as a nested, live tab.
4. A streams or waits for B's output and exit code, then continues.
5. The human watches B work, and can click into B and type, or kill it.

Sub-agents are themselves webterm sessions, so they inherit the same env and can
recursively spawn their own children — a tree, bounded by a depth guard.

## 2. Goals and non-goals

**Goals**

1. **Agents can spawn sub-agents.** A local process can create a session that
   runs an agent command, linked to its parent, and get back a handle.
2. **Sub-agents are visible by default.** They appear nested under the parent in
   the sidebar with live status, and the human can take them over.
3. **Parents can delegate and collect.** Spawn, send a task, stream output, wait
   for completion, read exit code and scrollback.
4. **Recursion is safe.** Depth and session caps prevent a fork bomb; cleanup is
   predictable.
5. **Local only, always.** The programmatic control plane is reachable only by
   local processes over an `AF_UNIX` socket guarded by filesystem permissions and
   peer credentials. There is no TCP variant and no flag that exposes it. A
   network client can never issue a control command. (The browser's own remote
   access is a separate, unchanged path — see §5.10.)
6. **Standard library only.** No new runtime dependencies. Client is a thin
   stdlib module plus a CLI.
7. The browser protocol and the agent protocol share one dispatcher, so features
   never diverge.

**Non-goals (for v1)**

- A server-side terminal emulator. `capture`/`wait --text` operate on the raw
  stream (with optional ANSI stripping), not on a rendered screen (§11, Q3).
- A full agent framework: no model routing, no task queue, no memory. v1 gives
  the *substrate* (spawn/read/wait) plus agent profiles; the orchestration brain
  stays in the parent agent.
- Remote (network) programmatic access.
- Windows. The core is POSIX `forkpty`; the socket follows suit.

## 3. What a sub-agent is (data model)

A sub-agent is an ordinary `Session` with lineage metadata. No new runtime
entity.

| Field | Meaning |
|---|---|
| `id` | Session id, opaque, unchanged. |
| `parent_id` | Session that spawned it, or `null` for a root. |
| `depth` | `0` for a human root; `parent.depth + 1` for a child. |
| `origin` | `"user"` (created in the browser) or `"agent"` (created over the socket). |
| `role` | `"shell"` or `"agent"`. Agents are flagged for the UI badge. |
| `agent` | Agent profile name (`pi`, `claude`, …), or `null`. |
| `task` | Short task label supplied by the parent, shown in the tab. |
| `visibility` | `"public"` (browser + socket) or `"private"` (socket only). |
| `created_at` | For TTL/GC and sorting. |
| `alive` / `exit_code` | Already in `Session.info()`. |

Environment exported to every child (in addition to the existing shell env):

| Var | Value |
|---|---|
| `WEBTERM_SOCKET` | Control socket path. |
| `WEBTERM_URL` | Browser base URL. |
| `WEBTERM_SESSION_ID` | The session the process is running in. |
| `WEBTERM_PARENT_SESSION` | Its parent, or empty for a root. |
| `WEBTERM_DEPTH` | Depth, for client-side guards and diagnostics. |
| `WEBTERM_AGENT` | Agent profile name, when it is an agent session. |

This is the `$TMUX` / `$WEZTERM_UNIX_SOCKET` trick: any agent — `pi`, Claude
Code, Codex, a shell script — discovers the control plane and its own place in
the tree with zero configuration. `webtermctl` and the Python client read these
vars to auto-fill `parent_id`, so "spawn a sub-agent" is one command.

## 4. Prior art: what to borrow, what to leave

| System | Model | Borrow | Leave |
|---|---|---|---|
| **tmux control mode** (`-CC`) | One long-lived text connection; `%output` notifications; `%begin…%end` guards; flow control and format subscriptions | One connection multiplexing all sessions; async notifications; **absolute offsets are our `%output` pane ids** | Ad-hoc `%`-line grammar and octal escaping |
| **Zellij** `zellij action` + `subscribe` | Subprocess per action; JSON on stdout; `--block-until-exit*`; `subscribe --format json`; `dump-screen` | Explicit blocking/wait flags; JSON results; stream + snapshot; `dump-screen` ≈ our `capture` | Subprocess-per-call; plugin/KDL worldview |
| **WezTerm** `wezterm cli` | Client → mux over a Unix socket; `$WEZTERM_UNIX_SOCKET`; `spawn`/`send-text`/`get-text`/`list` | Env-var discovery; spawn/send/get/list verbs; stable ids | GUI pane/window model (splits) |
| **kitty** `kitten @` | `--listen-on unix:…`; `--to unix:…`; `send-text --match` | Unix socket as the control surface; thin CLI | `@` command language; window matchers |
| **AgentAPI** (Coder) | HTTP+SSE over a terminal emulator; splits TUI output into messages; `/messages`, `/status`, `/events` | Run/read/wait loop and status (`stable`/`running`); SSE-style streaming | Deprecated; bolts an emulator + parser onto one agent |
| **tmux MCP** (libtmux-mcp) | MCP tools: `run_command`, `send_keys`, `capture_since`, `wait_for_text`, `wait_for_channel`, `snapshot_pane` | **The agent tool vocabulary**; offsets for `capture_since`; blocking `wait_*` | Requiring tmux; huge surface; no lineage |
| **VS Code terminal + shell integration** | In-process API; shell integration marks command boundaries | The value of knowing command start/end/exit code, not raw bytes | Editor coupling |
| **Herdr / agent-orchestration UIs** | Job/tree views of agent runs | **Lineage + live status in the sidebar**; a tree is the right mental model | Their own runtime; we reuse sessions |

The convergence: one local socket, a small verb set (spawn/run/send/capture/
wait/list/kill), machine-readable results, resumable streaming, env-var
discovery — **plus lineage**, which most prior art lacks and which is the point
here.

## 5. Design

### 5.1 Layer 0 — core primitives (in `session.py`)

Extend `Session`:

- `_exit_event: asyncio.Event`, set in `_mark_dead`; `await session.wait(timeout)`
  returns the exit code (or `None` on timeout). Replaces polling `session.dead`.
- `capture(since=0, *, strip_ansi=False) -> (start, end, bytes)`, over the
  existing `replay_since`.
- `wait_for_text(pattern, *, since=0, timeout, strip_ansi=True)`: scan the
  retained window, then attach a lightweight `_TextWaiter` subscriber that
  matches each `offer_output` chunk and sets an event. Returns `(matched,
  offset, context)`.
- `wait_for_idle(quiet_ms, *, timeout)`: subscriber that resets a timer on each
  chunk. This is how you detect "the agent stopped typing", which is often
  better than pattern matching a TUI.
- Lineage/metadata fields from §3, added to `info()`.

Extend `SessionManager`:

- `spawn(...)` / `run(...)` / `create(...)` / `create_single(...)` accept
  `parent_id`, `origin`, `role`, `agent`, `task`, `visibility`, `cwd`, `env`.
- `children_of(session_id)`, `descendants_of(session_id)` for the tree.
- `_prepare_child` merges `os.environ`, base env, `child_env`, per-call env, and
  the computed `WEBTERM_*` variables. The child id is computed as
  `f"{counter}-{os.getpid()}"` **inside** the child, so `WEBTERM_SESSION_ID` can
  be exported before `exec`.
- Recursion guard: `spawn`/`create`/`run` with a parent at or beyond
  `--max-agent-depth` fails with `depth_exceeded`; a global `--max-sessions` cap
  fails with `too_many_sessions`.
- Retention GC: a periodic task (default 60 s) closes finished retained one-shot
  sessions older than `--retained-ttl`, and caps retained finished sessions
  (`--max-retained`).

### 5.2 Layer 1 — transport abstraction

`ClientConnection` is welded to `WebSocket`. Split it:

```
Transport (Protocol):
    async def send_json(obj) -> None
    async def send_binary(data: bytes) -> None
    async def recv() -> tuple[int, bytes] | None   # (opcode, payload)
    async def close() -> None
```

- `WebSocket` already satisfies this.
- New `FramedTransport` for the Unix socket: each frame is
  `u32 big-endian length | u8 kind | payload`; `kind = 1` is JSON text (same
  `type` grammar as the browser), `kind = 2` is the existing binary output frame
  (`[len(id)][id][8-byte offset][bytes]`). `recv` returns
  `(OP_TEXT|OP_BINARY, payload)` to match `WebSocket.recv`.

Rename `ClientConnection` → `SessionConnection(transport, manager, control,
*, peer)`; `peer` is `"browser"` or `"agent"` and affects `send_sessions`
filtering only. One dispatcher serves both; the browser contract is unchanged.

The control transport is **`AF_UNIX` only**. `--control-socket` takes a
filesystem path; a value that looks like a TCP address is rejected at startup.
The browser WebSocket is left exactly as it is (see §5.10).

### 5.3 Request / response

Add optional correlation to the existing grammar rather than a second protocol:

- Requests **may** carry `"id"` (string or int).
- Server replies `{"type":"result","id":…,"result":{…}}` or
  `{"type":"error","id":…,"error":{"code":…,"message":…}}`.
- Notifications stay as today: `sessions`, `attached`, `exit`, `restarting`,
  plus binary output frames. Add `session` for create/rename/spawn updates and
  `tree` optionally.

Error codes: `bad_request`, `not_found`, `timeout`, `depth_exceeded`,
`too_many_sessions`, `busy`, `internal`.

### 5.4 Verbs

Shared with the browser (now reply-capable):

| Verb | Params | Result |
|---|---|---|
| `list` | `parent_id?`, `role?`, `include_private?` | `{sessions:[info]}` |
| `create` | `cols, rows, title?, command?, cwd?, env?, visibility?, parent_id?` | `{session}` |
| `kill` | `session_id`, `tree?` (default `true`) | `{ok, killed:[ids]}` |
| `rename` | `session_id, title` | `{session}` |
| `input` | `session_id, data` | `{ok}` |
| `resize` | `session_id, cols, rows` | `{ok}` |
| `subscribe` | `session_id, since?` | `{session}` + binary stream |
| `unsubscribe` | `session_id` | `{ok}` |
| `restart` | — | `{ok}` then `restarting` |

Agent/sub-agent extensions:

| Verb | Params | Result |
|---|---|---|
| **`spawn`** | `agent?, task?, command?, mode?` (`print`\|`interactive`), `title?, cwd?, env?, cols?, rows?, parent_id?, visibility?, wait?` | `wait:false` → `{session_id, session}` + stream; `wait:true` → `{session_id, exit_code, output_base64, output_text?, truncated, total}` |
| `run` | `command, …` | as above, without an agent profile |
| `send` | `session_id, data, submit?` | `{ok}` (`submit:true` appends `\n`) |
| `capture` | `session_id, since?, format?` (`text`\|`base64`), `strip_ansi?` | `{start, end, text?/data_base64?}` |
| `wait` | `session_id, since?, until` (`exit`\|`text`\|`idle`), `pattern?, timeout?, quiet_ms?` | `{matched, exit_code?, offset?, context?}` |
| `close` | `session_id` | `{ok}` — drop a finished retained session |

`spawn` is the sub-agent primitive and the ergonomic center. `run` is `spawn`
without a profile. `wait:true` (default for `spawn` in `mode:"print"`) returns
the sub-agent's result in one call; `wait:false` plus `subscribe`/`wait` is the
streaming form for long or interactive children.

### 5.5 Agent profiles — the seed of the default agentic interface

`spawn` needs to know how to invoke an agent. Rather than hardcoding, webterm
carries named **agent profiles**:

```jsonc
// served to the client and used by the server for spawn()
"agents": [
  {
    "name": "pi",
    "print":       ["pi", "-p", "{task}"],   // one-shot: output + exit code
    "interactive": ["pi", "{task}"]          // TUI: stays until the human quits
  },
  {
    "name": "claude",
    "print": ["claude", "-p", "{task}"]
  }
]
```

- Configured with `--agent NAME=PRINT_COMMAND` (repeatable), or a JSON file via
  `--agents-file`, or auto-detected: if `pi` is on `PATH`, add a `pi` profile the
  way the existing `pi agent` launcher is added today.
- `{task}` is substituted; if a template has no `{task}`, the task is appended.
- `mode` selects `print` (one-shot, retained, result returned) or `interactive`
  (TUI, stays alive; the human can attach). If the profile has no `print`
  template and `mode` is `print`, `spawn` errors.
- `agent` and `task` become session metadata, so the tab shows `pi · run the
  tests` and the badge is automatic.

This profile list is the seam for the future default agentic interface
(§7): richer profiles can later add tool/permission flags, a default model,
timeouts, and a task schema without touching the transport.

### 5.6 Output, offsets, and waiting

- Output always flows as binary frames, so the existing offset logic (resume,
  gap detection, `buffer_start`) applies to parents reading children.
- `capture` embeds bytes as base64 in JSON; a `text` convenience decodes with
  `errors="replace"` and optionally strips ANSI/OSC.
- Per-connection queue stays bounded (2048); on overflow the connection closes,
  not corrupts, and the client reconnects with `since` to replay.
- `wait` never blocks the loop; it awaits an event fed by the subscriber
  callback. `wait --idle` is the robust way to detect a TUI agent pausing.
- Parallel orchestration (spawn N children, wait for all) is client-side in v1;
  the parent agent opens one connection, keeps N offsets, and `wait`s. A
  server-side `all_of`/`any_of` is a possible later addition (§11, Q4).

### 5.7 Discovery and environment

Socket path resolution, highest priority first (always `AF_UNIX`):

1. `--control-socket PATH`
2. `$WEBTERM_SOCKET`
3. `$XDG_RUNTIME_DIR/webterm/control.sock`
4. `/tmp/webterm-<uid>/control.sock` (directory `0700`)

On startup the server writes `$XDG_RUNTIME_DIR/webterm/server.json` with
`{socket, url, pid, version}`, and unlinks stale sockets on rebind. Every child
gets the `WEBTERM_*` env from §3, so sub-agent discovery is automatic. Because
the child's `WEBTERM_SESSION_ID` is set before `exec`, an agent can spawn a
child that itself spawns children, and the whole tree is addressable.

### 5.8 Visibility — the agent tree in the UI

Because `manager.notify()` broadcasts `sessions`, sub-agents already appear as
tabs with no protocol change. The work is presentation and control:

- **Tree sidebar.** Render sessions as a tree by `parent_id`; children indent or
  collapse under the parent. This is the single most important UX change: the
  human wants to see *which agent spawned whom*.
- **Status.** Per-node badge: agent icon, `running` spinner, `exit 0` / `exit N`
  chip, elapsed time. Drive it from existing `alive`/`exit_code` plus
  `created_at`; no new server state.
- **Takeover.** Clicking a sub-agent tab opens its live terminal; typing goes to
  the child, exactly like any session. The human can rescue or redirect a stuck
  sub-agent.
- **Parent affordance.** A parent session's tab can show `n children` and an
  expand/collapse control, and (future) a "new sub-agent" action.
- **Private children.** `visibility:"private"` keeps noisy automation out of the
  sidebar while remaining addressable over the socket. Default is **public**, so
  the requirement holds: what an agent spawns, the human can watch.
- **GC.** Finished `print` children are closed by TTL unless `keep:true`, so a
  long orchestration does not accumulate hundreds of dead tabs.

### 5.9 Recursion, limits, and lifecycle

Sub-agents can spawn sub-agents; without guards that is a fork bomb.

- `--max-agent-depth` (default 5): `spawn`/`create`/`run` with a parent at or
  beyond the limit returns `depth_exceeded`. `WEBTERM_DEPTH` lets clients
  pre-check and degrade gracefully.
- `--max-sessions` (default 64): a global cap; exceeding it returns
  `too_many_sessions`. Counts live and retained sessions.
- **Cascade-kill by default.** `kill` terminates a session **and all its
  descendants** (returning every id it killed); `tree:false` kills only the one
  session and orphans its children to the root. A dying parent does not
  auto-kill children (its exit is not a `kill`); cascade applies to explicit
  `kill` requests. This is the runaway-agent safety valve: one `kill --tree`
  stops an agent and everything it spawned.
- `ServerHandle.stop()` closes the socket, removes its file, and tears down
  sessions exactly as today.

### 5.10 Security — the control plane is local only

The rule: **programmatic control commands are never accepted from the network.**
Enforced in two independent layers. This applies to the new control plane; the
browser transport and its remote-access story are unchanged (note below).

**1. No network transport exists for control.** The agent API is an `AF_UNIX`
socket, not a TCP port. There is no TCP control listener, no `--control-host`,
no HTTP/SSE bridge, and no option that exposes it. `--control-socket none`
disables it. Network reachability of the UI (`--host 0.0.0.0`, Tailscale, a
reverse proxy) has zero effect on the control socket.

**2. Filesystem permissions and peer credentials.** Socket `0600` inside a
`0700` directory; stale sockets unlinked and rebound only after a failed connect.
On Linux require `SO_PEERCRED.uid == os.geteuid()`; on macOS/BSD use
`LOCAL_PEERCRED`/`getpeereid`; otherwise the `0600` mode is the boundary. Never
follow symlinks when creating the directory or binding. Bound request sizes and
queue depths as on the WebSocket path.

**Browsers are a separate, intentionally network-capable path.** The browser
WebSocket keeps its current behaviour, including remote access via `--token` +
TLS, Tailscale, or a reverse proxy. A remote browser is an authenticated human
driving their own session; that is the product. The control plane never shares
the browser's TCP listener, so hardening the socket cannot break remote access,
and exposing the UI cannot grant programmatic control.

### 5.11 In-process use

Everything above is usable in-process: the desktop host already holds a
`SessionManager`, and `ServerHandle` exposes it. The socket is one client among
others, so the Tauri app can spawn and inspect sub-agents directly, and tests
drive the API without a socket.

## 6. CLI and client library

Two thin, stdlib-only pieces:

- `webterm/control.py` — Unix socket server + framing.
- `webterm/client.py` — `ControlClient` (sync) with methods mirroring the verbs,
  auto-discovering the socket and `parent_id` from the `WEBTERM_*` env.
- `webterm/ctl.py` — the `webtermctl` console script:

```
webtermctl list [--tree] [--json]
webtermctl spawn --agent pi --task "run the tests" [--print|--interactive] [--wait] [--json]
webtermctl run   [--wait] [--json] [--text] CMD...
webtermctl new   [--title T] [--cwd D] [--command CMD] [--private]
webtermctl send  <id> TEXT [--enter]
webtermctl capture <id> [--since N] [--text]
webtermctl wait  <id> --exit | --text RE | --idle MS [--timeout S] [--json]
webtermctl kill  <id> [--tree]
webtermctl rename <id> T
```

`webtermctl spawn` is the one-liner a parent agent calls: it fills `parent_id`
from `$WEBTERM_SESSION_ID`, so `pi` (or any agent that can run a shell command)
gets sub-agents with no extra integration. `--json` emits raw results. A separate
console script avoids touching the existing `webterm` parser (no subcommands
today).

## 7. Future: webterm's default agentic interface

Once profiles exist, webterm can own the orchestration UX without owning the
intelligence:

- **Built-in profiles** for common agents (pi, Claude Code, Codex, Aider),
  auto-detected on `PATH`, replacing the single hardcoded `pi agent` launcher.
- **A "new sub-agent" action** on any session in the UI: pick an agent, type a
  task, watch the child appear nested beneath it. This is the human-facing mirror
  of the `spawn` verb.
- **A task board / agent tree view**: a dedicated panel listing running agents,
  their tasks, status, and outputs, with one-click attach, kill, and re-run.
- **Declarative task specs**: `spawn` gains a `tasks: [...]` form (fan-out) and
  profiles gain defaults (model, permissions, timeout, working dir, retries), so
  a parent can say "one agent per failing test" in a single call.
- **Optional MCP server** (`webterm-mcp`): expose `webterm_spawn`,
  `webterm_list`, `webterm_capture`, `webterm_wait`, `webterm_kill` as MCP tools,
  so agents that speak MCP get sub-agents with no shelling out. Stdlib JSON-RPC
  over stdio.

All of this rides on the same verbs; the substrate in §5 is the durable part.

## 8. Wire examples

Spawn a print-mode sub-agent and get its result (the `pi` case):

```jsonc
// →  (parent_id auto-filled from $WEBTERM_SESSION_ID by webtermctl)
{"id":"1","type":"spawn","agent":"pi","task":"run the test suite",
 "mode":"print","title":"tests","wait":true,"timeout":600}
// ←
{"type":"result","id":"1","result":{"session_id":"12-4310","exit_code":1,
 "output_base64":"…","truncated":false,"total":20481}}
```

Spawn an interactive child the human can drive, then stream it:

```jsonc
{"id":"2","type":"spawn","agent":"pi","task":"refactor auth","mode":"interactive",
 "parent_id":"7-4231","wait":false}
// ← {"type":"result","id":"2","result":{"session_id":"13-4315","session":{…}}}
// ← binary: [len("13-4315")][13-4315][u64 0][TUI bytes…]
```

Wait for it to pause, then read what it has produced:

```jsonc
{"id":"3","type":"wait","session_id":"13-4315","until":"idle","quiet_ms":1500,
 "timeout":120}
// ← {"type":"result","id":"3","result":{"matched":true,"offset":8123}}
{"id":"4","type":"capture","session_id":"13-4315","format":"text","strip_ansi":true}
```

List the whole tree:

```jsonc
{"id":"5","type":"list"}
// ← {"type":"result","id":"5","result":{"sessions":[
//      {"id":"7-4231","parent_id":null,"depth":0,"role":"agent","alive":true},
//      {"id":"13-4315","parent_id":"7-4231","depth":1,"role":"agent","task":"refactor auth"}]}}
```

## 9. Implementation plan

| Phase | Deliverable | Files | Tests |
|---|---|---|---|
| **0** | Core primitives: `Session.wait`, `wait_for_text`, `wait_for_idle`, `capture`, lineage fields, per-call `env`/`cwd`, `SessionManager.spawn`/`run`/`children_of`, recursion + session caps, retention GC, child env | `webterm/session.py`, `webterm/__main__.py` | extend `tests/test_session.py` |
| **1** | `Transport` protocol + `FramedTransport`; extract `SessionConnection`; request `id` → `result`/`error`; `send_sessions` filtering; **Unix control listener** start/stop; discovery + `server.json`; `WEBTERM_*` env; peer-credential check | `webterm/server.py`, `webterm/control.py`, `webterm/session.py`, `webterm/__main__.py` | new `tests/test_control.py`, `tests/controlclient.py`; bad peer uid refused; stale socket rebind; oversized frames rejected |
| **2** | `spawn` + agent profiles (`--agent`); `webterm/client.py` + `webtermctl` (`spawn/run/send/capture/wait/list/kill/rename`) | `webterm/client.py`, `webterm/ctl.py`, `webterm/__main__.py`, `pyproject.toml` | `tests/test_cli.py`, `tests/test_control.py`; nested spawn + depth cap |
| **3** | **Agent-tree sidebar**: nesting by `parent_id`, status badges, private filtering, TTL-closed children; docs (`docs/architecture.md`, `docs/backend.md`, `docs/frontend.md`, `README.md`, this file) | `webterm/static/app.js`, `webterm/static/style.css`, `docs/*` | manual + tree/JS checks; snapshot `list` has lineage |
| **4** | `webterm-mcp` stdio tools over the control socket | `webterm/mcp.py` | `tests/test_mcp.py` |
| **5** | Future: task fan-out (`tasks:[…]`), task board, built-in profiles, optional minimal VT parser for rendered capture | — | — |

Phase 0 and 1 carry the design risk; the tree UI (phase 3) is what makes the
user story real and should not be deferred past the CLI.

## 10. Decisions and alternatives considered

- **Why not tmux?** webterm's sessions *are* the multiplexer and already visible
  in the browser. Shelling out to tmux would create a second, invisible session
  pool and duplicate reaping/scrollback. We borrow the control-mode *model*.
- **Why lineage in the server, not just the client?** The human's view is the
  server's session list. If `parent_id`/`depth` live only in the parent agent's
  memory, the sidebar cannot draw the tree and a reconnecting browser loses the
  relationships. Lineage is shared state.
- **Why a Unix socket, not the WebSocket?** Local agents should not need HTTP,
  tokens, or a WS client, and the embedded desktop app has no way to hand a
  local process its per-launch token. A `0600` socket is the right local
  boundary, and it guarantees local-only control.
- **Why agent profiles?** Hardcoding `pi` would repeat the current single
  launcher. Profiles give a config seam for the future default agentic interface
  and keep agent-specific argv out of the transport.
- **Why `spawn` and `run` separately?** `spawn` names an agent (profile); `run`
  is the generic primitive. Both build on `create_single`/`create`; neither adds
  new runtime concepts.
- **Why offset-based capture?** It exists and makes parent reads lossless across
  reconnects; tmux MCP's `capture_since` validates the shape.

### 10.1 Decisions locked

- **CLI first.** Sub-agents ship through `webtermctl spawn` and the control
  socket before any MCP layer. An agent that can run a shell command (pi via its
  Bash tool) gets sub-agents with no integration work.
- **Cascade-kill.** `kill` terminates the subtree by default (`tree:false`
  opts out).
- **Tree view.** The sidebar renders sessions nested by `parent_id`; children
  collapse under the parent.

## 11. Open questions for discussion

1. **Default visibility of spawned children.** Always public (watch every
   sub-agent) vs. public with aggressive TTL vs. private unless asked. The user
   story says watch them; how loud should fan-out automation be?
2. **Lineage on kill.** **Resolved: cascade-kill by default** (`kill` kills the
   subtree; `tree:false` orphans). Confirm this is also what a dying parent
   should do — the plan still does *not* cascade on natural exit, only on an
   explicit `kill`.
3. **`wait --text` fidelity.** Raw-stream regex (cheap, can false-match a TUI) vs.
   `wait --idle` plus `capture` (robust, less precise) vs. a minimal VT screen
   (accurate, more code). Which is the v1 default for "the sub-agent finished"?
4. **Parallel fan-out.** Client-side `wait` loops in v1, or a server-side
   `wait` with `all_of`/`any_of` for `spawn --tasks`? Affects how `pi` delegates
   to many children.
5. **Profiles format.** `--agent NAME=CMD` flags only, or a JSON file with
   per-agent defaults (model, timeout, permissions) from the start?
6. **MCP timing.** **Resolved: CLI first** (phase 2), MCP later (phase 4).
   `pi` reaches `webtermctl` through its shell today, so MCP is an optimization,
   not a prerequisite.
7. **Attach UX.** Should `webtermctl attach <id>` give a human a real terminal on
   a sub-agent, or is the browser always the attach path? (AgentAPI has
   `attach`; it is convenient for debugging a stuck child.)
