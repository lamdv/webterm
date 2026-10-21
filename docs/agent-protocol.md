# Agent protocol and `AgentSession`

Status: **implemented end to end** — the JSONL protocol, generic `AgentSession`,
manager spawn path, fake-agent conformance suite, pi translation layer, the
canonical `agent` command envelope, history paging, shadowed extension UI, and
native agent sessions over the browser and control sockets. `kind` is `"agent"`
for every structured session and `adapter` selects the codec: pi is `"pi"`,
anything else speaks the canonical protocol itself. pi's *process* machinery is
still its own; only the client-facing surface is unified.

This document defines the agent-agnostic layer. The goal is to separate three
concerns that pi currently handles together:

1. **Session orchestration** — process lifecycle, runs, transcripts, replay,
   dialogs, lineage, limits, and the browser/control wire.
2. **Agent semantics** — how a particular agent stores sessions, discovers
   models, compacts context, forks, or renders its special records.
3. **Presentation** — how a client draws messages, tool calls, and dialogs.

webterm owns (1). An adapter owns (2). Capabilities and a small `raw` channel
keep (3) from forcing every agent into the same shape.

## Why a canonical protocol

pi's RPC mode is a good reference, but it is not the contract:

- its replay cursor is pi's entry id, which assumes an append-only entry store;
- its message and tool records describe pi's internal message shapes;
- its extension UI is pi-specific;
- its session files are pi-specific.

MVA should not have to fake pi, and a future agent should not have to implement
pi's dialect. The canonical protocol is webterm-owned and deliberately small.
Adapters translate native protocols into it.

## Records

All records are single-line JSON objects terminated by LF. A UTF-8 byte stream
is split only on LF; Unicode line separators inside strings are data. stdout is
protocol-only. stderr is diagnostics and never parsed as protocol.

There are four record families, distinguished by `type`:

| `type` | Direction | Purpose |
|---|---|---|
| `hello` | agent → webterm | startup declaration and capabilities |
| `response` | agent → webterm | answer to a command, correlated by `id` |
| `event` | agent → webterm | canonical activity stream |
| `dialog_request` | agent → webterm | ask the human a question |
| `dialog_response` | webterm → agent | answer a dialog |
| `dialog_resolved` | either → other | a dialog is no longer pending |

Commands are flat records whose `type` is the command name:

```json
{"type":"prompt","id":"c1","message":"run the tests"}
```

### Hello

The agent sends exactly one `hello` immediately after startup:

```json
{"type":"hello","protocol":1,"agent":"mva","version":"0.1.0",
 "session_id":"optional-agent-session-id",
 "capabilities":{
   "prompt":true,
   "abort":true,
   "steering":false,
   "follow_up":false,
   "queue":false,
   "dialogs":["confirm"],
   "models":{"list":true,"set":true,"thinking":false,"levels":[]},
   "system_prompt":false,
   "snapshot":true,
   "history":{"paging":true},
   "resume":true,
   "export":{"native":false},
   "usage":{"tokens":true,"cost":false},
   "tools":{"streaming":true,"updates":true},
   "attachments":{"images":false},
   "raw":false
 }}
```

`protocol` must be an accepted major version (`1` today). A missing or invalid
hello is a startup failure; `AgentSession.start()` reports the stderr tail and
the spawner closes the child. Capability fields are advisory: `false` means the
client hides the control, not that the agent is defective.

Capability groups:

| Capability | Meaning |
|---|---|
| `prompt`, `abort` | basic run control |
| `steering`, `follow_up`, `queue` | mid-run input |
| `dialogs` | supported `dialog_request.kind` values |
| `models.list`, `models.set`, `thinking` | model/thinking selection |
| `models.levels` | thinking levels `set_thinking` accepts; empty means the adapter has no fixed vocabulary and the client falls back to `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| `system_prompt` | the adapter accepts `set_system_prompt` |
| `snapshot`, `history.paging` | transcript hydration and backwards paging |
| `resume`, `export.native` | agent-owned session files and exports |
| `usage.tokens`, `usage.cost` | usage reporting |
| `tools.streaming`, `tools.updates` | live tool args / output |
| `fork`, `clone`, `compaction` | agent-specific session operations |
| `attachments.images` | image input |
| `raw` | native command escape hatch |

### Commands

Core commands every agent should implement:

| Command | Fields | Response `data` |
|---|---|---|
| `prompt` | `message` | `{"disposition":"started" \| "queued" \| "handled"}` |
| `abort` | — | `{}` |
| `get_state` | — | agent-defined state (model, busy, session name, ...) |
| `get_snapshot` | `limit` | snapshot (below) |
| `get_history` | `before`, `limit` | `{entries, has_more, first_entry_id}` |
| `list_models` | `refresh` | `{models, default, fetched_at?}` |
| `set_model` | `provider?`, `model` | `{model, provider?}` |
| `shutdown` | — | `{}` |

Capability-gated commands: `steer`, `follow_up`, `clear_queue`,
`set_thinking`, `set_system_prompt`, `set_session_name`, `compact`, `fork`,
`clone`, `switch_session`, `export`, and `raw` (whose `command` field is passed
to the native protocol). `set_thinking` carries `level`; the adapter advertises
the levels it understands in `models.levels`. `set_system_prompt` carries
`text`, and an empty string clears the prompt.

`webterm-<pid>-<n>` is the default command id. Agents must echo it in the
response. A success response means *accepted*, not finished; completion is
`run.settled`.

```json
{"type":"response","id":"c1","command":"prompt","success":true,
 "data":{"disposition":"started"}}
```

A failed command returns `success:false` and a human-readable `error`:

```json
{"type":"response","id":"c1","command":"set_model","success":false,
 "error":"unknown model"}
```

### Browser envelope

The protocol describes agent pipes. A browser wraps the same flat record in an
`agent` message and gets a canonical response back, so it never speaks an
adapter's dialect:

```json
{"type":"agent","id":"r7","session_id":"1-3",
 "command":{"type":"prompt","message":"run the tests"}}
```

```json
{"type":"result","id":"r7","result":{"agent_response":
 {"type":"response","id":"r7","command":"prompt","success":true,
  "data":{"disposition":"started"}}}}
```

The outer `id` is the transport correlation id and becomes the canonical `id`
when it is a string or number; the server generates `webterm-<pid>-<n>` when it
is missing. Local validation failures use the transport error shape
(`{"type":"error","error":{"code":...,"message":...}}`): an unknown
session, a session that is not a structured agent, an unknown command name, or
a malformed field. A command the *agent* refuses is a `success:false` response
with `error`, so the client can show the agent's own wording.

A mid-run prompt is expressed with the canonical `steer` or `follow_up`
command, not an adapter-specific field. The `prompt` disposition is `started`
or `handled`; the mid-run forms report `queued` because the agent chose to
queue rather than start a run.

The current pi adapter answers:

| Command | Adapter behavior |
|---|---|
| `prompt`, `steer`, `follow_up` | one pi prompt; the mid-run forms set `streamingBehavior` |
| `abort`, `get_state`, `set_model`, `set_thinking`, `set_session_name`, `compact`, `clear_queue` | one pi RPC call, with the response data normalized |
| `get_snapshot` | newest transcript page plus `cursor.seq`; also emits `agent_resync` (always for native agents, for pi only when the connection opted in) |
| `get_history` | one backwards page before `before` |
| `list_models` | the cached model probe for the session's cwd |
| `dialog_response` | the server-owned dialog queue; `approved` becomes pi's `confirmed` |
| `shutdown` | graceful close |
| `raw` | passes the native command through, except server-owned `extension_ui_response` |

### Events

An event record is:

```json
{"type":"event","event":"message.delta","message_id":"m1",
 "block_index":0,"block_type":"text","delta":"hello"}
```

The core assigns `seq`; agents must not set it. Sequence numbers are monotonic
per session and are the replay cursor for live events. Unknown event names are
allowed and are forwarded unchanged, so a newer agent can add records without
breaking an older webterm.

Canonical events:

| Event | Key fields | Purpose |
|---|---|---|
| `run.started` | `run_id` | a run began |
| `run.finished` | `run_id`, `reason` (`stop`, `aborted`, `error`) | one run ended |
| `run.settled` | `run_id` | the agent will not continue automatically |
| `message.started` | `message` | a new transcript message |
| `message.delta` | `message_id`, `block_index`, `block_type`, `delta` | stream text/thinking |
| `message.block` | `message_id`, `block_index`, `block` | a complete block, e.g. a tool call |
| `message.finished` | `message` | the authoritative message |
| `tool.started` | `call_id`, `name`, `args` | tool execution began |
| `tool.updated` | `call_id`, `delta` or `output` | live tool output |
| `tool.finished` | `call_id`, `output`, `is_error` | tool execution ended |
| `queue.updated` | `steering`, `follow_up` | pending input |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost` | usage |
| `session.info` | `model`, `session_file`, `name`, ... | session metadata changed |
| `notice` | `level`, `code`, `text` | banner/compaction/retry notice |
| `raw` | `protocol`, `record` | adapter-specific record |

Content blocks: `text`, `thinking`, `tool_call`, `image`, `file`. A tool call
block carries `call_id`, `name`, and `args`; `tool.*` events carry execution
state for the same `call_id`. A persisted snapshot may fold the result into the
block or keep a separate `tool_result` entry, but the `call_id` join is stable.

The `event` field, not `name`, holds the canonical event name. This leaves
`name` free for a tool name in `tool.started`.

### Dialogs

```json
{"type":"dialog_request","id":"d1","kind":"confirm",
 "title":"Proceed?","message":"run the dangerous thing",
 "options":["yes","no"],"timeout_ms":30000}
```

`kind` is one of `confirm`, `select`, `input`, `editor`, `notify`. The core
queues the request, broadcasts it to subscribers, replays it on attach, and
accepts one response:

```json
{"type":"dialog_response","id":"d1","response":{"approved":true}}
```

On timeout the core sends `{"cancelled":true}` and broadcasts a resolution with
reason `timeout`. The dialog queue is server-owned; a disconnecting browser does
not cancel a browser-origin dialog. Unattended agent-origin policy is a server
concern, not a session concern.

## Snapshot, replay, and resume

`get_snapshot` is the hydration primitive, not `get_history`. It returns the
newest page plus enough state for a client to render immediately:

```json
{"entries":[...],
 "has_more":true,
 "first_entry_id":"entry-100",
 "state":{"model":"...","streaming":false},
 "dialogs":[...],
 "capabilities":{...}}
```

The core adds `cursor.seq`: the event sequence at the moment the snapshot
request was sent. An attaching client should:

1. subscribe and buffer live events;
2. receive the snapshot;
3. drop buffered events with `seq <= cursor.seq`;
4. apply the rest.

The agent's snapshot must reflect state at or before the request. Agents that
cannot provide a stable cursor may return no cursor; the core then relies on
message/entry ids and replaces the transcript on attach. `get_history` is a
backwards page:

```json
{"entries":[...],"has_more":true,"first_entry_id":"entry-80"}
```

Entry shape used by the snapshot and page:

```json
{"id":"entry-1","role":"user",
 "content":[{"type":"text","text":"hello"}],
 "timestamp":"2024-01-01T00:00:00Z"}
```

Session storage stays adapter-owned. webterm provides the managed root and
validates paths; it does not parse an agent's files. `resume` is advertised only
when the adapter can reopen a session by file/id.

## Process and lifecycle

`AgentSession` mirrors `Session`'s lifecycle surface without a PTY:

| Concern | `Session` (shell) | `AgentSession` |
|---|---|---|
| Process | `forkpty` | `fork` + pipes + `execvpe` |
| Output | raw bytes | JSONL records |
| Input | keystrokes | commands |
| Replay | byte offset | `seq` + snapshot cursor |
| Renderer | xterm.js | canonical transcript |
| Resize | `TIOCSWINSZ` | no-op |
| Liveness | SIGCHLD reaper | SIGCHLD reaper |

Lifecycle states: `starting` (before hello), `ready`, `running` (between
`run.started` and `run.settled`), `closing`, `dead`. `needs_input` is an overlay
while dialogs are pending.

Two-phase close mirrors pi: close stdin, `SIGTERM` the process group, `SIGKILL`
if needed, then local teardown. The process group is used so tool subprocesses
die with the agent. stdout EOF alone does not mean dead; the exit status from
the reaper does. `force_close()` is for server shutdown and marks the session
dead immediately.

Protocol failures are counted. A malformed record is logged and ignored; too
many failures close the session. An invalid handshake fails `start()` rather
than hanging the spawner.

## Implementation

```
webterm/agent/
├── __init__.py
├── protocol.py     # capabilities, hello, validation, event names
├── process.py      # fork/exec into pipes, shared child environment
└── session.py      # AgentSession: commands, events, replay, dialogs, close
```

`SessionManager.create_agent(argv, ...)` forks the child, registers it with the
same reaper and lineage/limit logic as PTY and pi sessions, constructs the
`AgentSession`, waits for `hello`, and publishes it. An ephemeral agent is
reaped but never published or counted against limits.

The reference test agent is `tests/fake_agent.py`; `tests/test_agent_session.py`
covers hello/capabilities, streaming, tools, usage, snapshots, history, models,
dialogs (answer and timeout), abort, large tool output, crash, handshake
timeout, unsupported protocol, and close.

### pi translation

`webterm/integration/pi/canonical.py` is the first adapter translation layer.
It is side-effect free; the server calls it and the canonical view consumes the
result:

- `PiCanonicalTranslator.feed()` maps pi session events (`agent_*`,
  `message_*`, `tool_execution_*`, `queue_update`, compaction/retry, extension
  errors) to canonical events. pi message deltas have no message id, so the
  translator assigns live ids; a snapshot later replaces them with pi entry
  ids.
- `pi_snapshot_to_canonical()` and `pi_history_to_canonical()` convert pi
  entries to canonical entries, keeping pi entry ids as the paging cursor.
- `pi_command_from_canonical()` and `canonical_response()` translate the
  browser's canonical commands into pi RPC and normalise the answers.
- `pi_dialog_to_canonical()`, `pi_ui_request_to_events()`, and
  `pi_ui_resolved_to_canonical()` map extension UI. Blocking dialogs become
  canonical dialogs; `notify`/`setTitle` become events; other fire-and-forget
  UI state is preserved as `raw`.
- `pi_models_to_canonical()` normalizes the model probe payload.
- `PI_CAPABILITIES` declares pi's full surface, including steering, fork,
  clone, compaction, export, and images.

`tests/test_pi_canonical.py` checks the mapping against both synthetic
realistic pi records and the `fake_pi.py` process.
`webterm/integration/pi/codec.py` wraps that translation as `PiCodec`, the
adapter the server sees.

### Server surface

Every structured session is `kind: "agent"`, and its `adapter` selects a codec.
Native agents are canonical end to end; pi also keeps the legacy stream for its
own view:

- Send `{"type":"agent_api","enabled":true}` to enable pi's canonical shadow
  stream for a connection, or pass `"agent_api":true` with `subscribe`. Native
  agents need no flag: their events are the only stream they have.
- `agent_resync` mirrors `pi_resync`: `{type, session_id, entries, has_more,
  first_entry_id, state, dialogs, ui, commands, capabilities, cursor}`.
- `agent_event` mirrors each translated `pi_event`: `{type, session_id, seq,
  event}`. Several canonical events may share one pi `seq` when a pi record
  expands into more than one canonical event.
- `agent_dialog_request` and `agent_dialog_resolved` mirror
  `pi_ui_request`/`pi_ui_resolved` for blocking extension dialogs.
- Fire-and-forget extension UI is shadowed too: extension UI records never
  reach the event stream, so `notify` becomes a `notice`, `setTitle` a
  `session.info`, and `setStatus`/`setWidget` a `raw` event, all with the
  session's current `seq`.
- Commands travel in the `agent` envelope described above; a session-level
  `get_snapshot` or `list_models` answer works even when the connection never
  opted in, but only an opted-in connection is also pushed `agent_resync` and
  live `agent_event`s.
- `pi_event`, `pi_resync`, `pi_ui_request`, and `pi_ui_resolved` are still sent
  exactly as before. A client that never enables `agent_api` sees byte-for-byte
  the same pi protocol (plus no canonical traffic).
- Enabling `agent_api` after subscribing sends an `agent_resync` for each
  already-attached pi session, then continues live.

The browser can opt in with `?agentui=1`. `agent.js` renders the canonical
transcript, dialogs, and backwards history paging (`agent.get_history` through
the envelope), and `app.js` drives it with `prompt`/`steer`/`follow_up`,
`abort`, `dialog_response`, and `get_history`. The legacy pi view remains the
default for pi sessions and keeps using `pi` / `pi_ui_response` / `pi_history`.

A native agent (`adapter` other than `"pi"`) has no legacy channel: it is
subscribed with `agent_resync` and live `agent_event`s immediately, and the
canonical view is its only renderer. `agent_api` therefore only gates pi's
shadow stream.

### Codecs

`session.kind` is `"shell"` or `"agent"`; `session.adapter` names the dialect
behind a structured session. The server reaches every adapter through one seam,
`webterm/agent/codec.py`:

| Adapter | Codec | Talks to | Capabilities |
|---|---|---|---|
| `"generic"` (any unregistered name) | `NativeCodec` | `AgentSession`, whose child already speaks canonical JSONL | `AgentSession.capabilities` |
| `"llama"` | `NativeCodec` | `AgentSession` running `webterm.integration.llama` against an OpenAI-compatible llama server | its declared chat capabilities, including model/thinking selection and a system prompt |
| `"pi"` | `PiCodec` | `PiSession`, whose child speaks pi RPC | `PI_CAPABILITIES` |

A codec answers `capabilities`, `snapshot`, `history`, `models`,
`resolve_dialog`, `command`, and `result` (the structured result of a spawn),
and owns the set of commands its adapter accepts. `server.py` looks the codec up
from `session.adapter` once and never branches on pi again; adding an adapter
means adding a codec, plus a translator module when its child speaks another
dialect.

## Next steps

1. **MVA reference implementation**: `mva-core` speaks this protocol directly
   over `mva --mode rpc`; the worker-thread run loop, per-session cancellation,
   dialog round trip, and JSONL session store are the prerequisites. The
   adapter name is `"mva"` (falling back to `NativeCodec`) once it announces a
   canonical `hello`.
2. **Share pi's process machinery**: `PiSession` keeps its own fork/replay/dialog
   code while `AgentSession` has the canonical equivalent. Folding pi's process
   handling behind `AgentSession` with a codec on the child side is mechanical
   cleanup now that both speak the same client surface.
3. **Capability-driven UI**: hide queue/steer, model picker, thinking, cost,
   fork/tree, and export when the adapter does not advertise them.

## Non-goals

- A universal agent framework: webterm does not route models, queue tasks, or
  run workflows.
- A universal session file format: adapters own their storage.
- Forcing parity: a missing capability hides a control; it does not fake it.
- A remote control plane: the agent protocol runs over local child pipes, just
  like the existing pi integration. Browser access keeps the existing
  token/TLS/reverse-proxy story.
