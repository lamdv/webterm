# Agent protocol and `AgentSession`

Status: **stage 1 implemented** — canonical JSONL protocol, generic
`webterm.agent.AgentSession`, manager spawn path, and a fake-agent conformance
suite. pi still runs through its existing `PiSession`; no browser or
`webtermctl` surface is wired to the generic layer yet.

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
   "models":{"list":true,"set":true,"thinking":false},
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
`set_thinking`, `set_session_name`, `compact`, `fork`, `clone`,
`switch_session`, `export`, and `raw` (whose `command` field is passed to the
native protocol).

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

## Next steps

1. **`PiAdapter`**: translate pi's RPC records into canonical events, and
   canonical commands into pi commands. Keep pi's entry ids for replay, and
   keep fork/tree/compaction/dialogs as capability-gated pi features.
2. **Server/browser surface**: add `agent_*` messages and a canonical reducer.
   Translate pi events server-side first so the existing UI can validate the
   schema without a backend rewrite.
3. **MVA reference implementation**: `mva-core` speaks this protocol directly
   over `mva --mode rpc`; the worker-thread run loop, per-session cancellation,
   dialog round trip, and JSONL session store are the prerequisites.
4. **Capability-driven UI**: hide queue/steer, model picker, thinking, cost,
   fork/tree, and export when the adapter does not advertise them.

## Non-goals

- A universal agent framework: webterm does not route models, queue tasks, or
  run workflows.
- A universal session file format: adapters own their storage.
- Forcing parity: a missing capability hides a control; it does not fake it.
- A remote control plane: the agent protocol runs over local child pipes, just
  like the existing pi integration. Browser access keeps the existing
  token/TLS/reverse-proxy story.
