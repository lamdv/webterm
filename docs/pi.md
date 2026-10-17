# pi GUI mode

Design decisions for running pi as a structured, first-class session kind in
webterm, alongside ordinary shell sessions. The goal is a browser chat client
for a headless `pi --mode rpc` process: remote, persistent, and able to show
messages, tool calls, and agent status without emulating the TUI in xterm.

Status: **design accepted; P0 server side and an initial browser chat view implemented.**
Tracking issue: #10. For the
existing PTY-based pi launcher, nothing changes: it stays as the escape hatch
for TUI-only features.

## Goals

- A `kind: "pi"` session whose view is a chat/console UI, in the same sidebar
  and tree as shell sessions.
- Server-owned sessions that survive browser refresh, sleep, and network
  changes, using the same auth and remote-access story as shells.
- Structured rendering: streamed text, thinking blocks, tool cards, queue
  state, usage, and extension dialogs.
- No new Python runtime dependencies and no frontend build step.
- The same trust level as the shell webterm already hands out.

Out of scope for now: pi TUI emulation, multi-user accounts, extension custom
renderers (`ctx.ui.custom()`), and a web extension API.

## Session model

`SessionManager` becomes a registry for two session kinds. `kind` is part of
`Session.info()` and every `sessions` / `attached` payload.

| Concern | shell | pi |
|---|---|---|
| Process | PTY child from `forkpty` | `fork` + pipes + `execvpe` |
| Output | raw bytes | JSONL records |
| Replay cursor | absolute byte offset | entry id plus live sequence |
| Replay source | scrollback byte ring | `get_entries`, then live events |
| Input | keystrokes | RPC commands |
| Resize | `TIOCSWINSZ` | no-op |
| Renderer | xterm.js | DOM chat view |
| Liveness | SIGCHLD reap | SIGCHLD reap |

`PiSession` is a sibling of `Session`, not a subclass. It shares the lifecycle
surface: `id`, `pid`, `title`, `info()`, `attach`, `detach`, `subscribers`,
`dead`, `exit_code`, `wait()`, lineage metadata, and `retain_on_exit`. It does
not implement the terminal-only surface: `buffer`, `replay_since`, byte
`write`, and `resize`.

`SessionManager` types its registry as `Session | PiSession` and only calls
the shared lifecycle surface. If a third kind appears, a `SessionLike` protocol
can replace the union.

Additional `info()` fields:

- `kind`: `"shell"` or `"pi"`.
- `closing`: true while a two-phase shutdown is in progress.
- `trusted`: `true`, `false`, or `null` for pi sessions, for the project-trust
  chip.

## Process model

Pi is spawned with `fork`/`execvpe` and raw pipes, not
`asyncio.create_subprocess_exec`. The existing reaper calls
`os.waitpid(-1, os.WNOHANG)` for every child; an asyncio subprocess watcher
would race it and can lose the exit status or log an unknown pid. Using the
same child path as PTY sessions keeps `_reap_children` as the single reaper.

In the child:

1. `setsid()` so the pi process is the leader of its own process group.
2. Create three pipes with `os.pipe2(O_CLOEXEC)`: stdin, stdout, stderr.
3. `dup2` the child ends onto fds 0, 1, and 2; close every other inherited fd.
4. Reset signals, merge environment, and `chdir` using the same helper as the
   PTY path (`_prepare_child`). `WEBTERM_*` discovery keeps working.
5. `execvpe(pi, ...)`; exit 127 on failure.

In the parent:

- Set the stdout/stderr read ends and the stdin write end non-blocking.
- Register stdout and stderr with `loop.add_reader`; use the existing
  `add_writer` buffered-write pattern for stdin.
- Register the pid in `_by_pid` so SIGCHLD routes to
  `PiSession._on_child_exit(code)`.
- Parse stdout as strict JSONL: buffer bytes and split only on LF, never on
  Unicode line separators. Cap the partial-line buffer; an overlong record is
  a protocol error, not an excuse to grow without bound.
- Parse stderr separately and log it. It is never protocol data.

Lifecycle rules:

- EOF on stdout alone does not mark the session dead. Wait for the child exit.
- `_on_child_exit` drains stdout and stderr, records the exit code, then runs
  the common `_mark_dead()` path.
- `agent_end` is one low-level run. Only `agent_settled` means pi will not
  continue automatically. `prompt` returning `disposition: "started"`,
  `"queued"`, or `"handled"` is acceptance, not completion.

### Two-phase close

`Session.close()` is synchronous today. Pi gets a short state machine instead
of an immediate close:

```
running -> closing -> terminating -> killing -> force-dead
```

- `close()` is idempotent, sets `closing`, clears `retain_on_exit`, closes the
  stdin write end, and schedules timers.
- `SIGTERM` to the process group at about 0.5 s; `SIGKILL` at about 2.5 s; a
  local force-teardown at about 4 s if the process cannot be reaped.
- Timers use `loop.call_later` handles stored on the session, not tasks, and
  re-check `dead` before acting.
- `_on_child_exit` cancels the timers, drains the pipes, and marks the session
  dead. Readers stay registered until then so final records still arrive.
- `kill()` may close a session that is already `closing`; it must not schedule
  a second shutdown.
- `close_all()` uses a force path: `SIGKILL` the group, mark dead immediately,
  then remove the reaper. Server shutdown does not need a graceful pi flush.
- Always signal the process group (`killpg(pid, sig)`) so pi's bash-tool
  children die with it. Fall back to `kill(pid, sig)` if the group is gone.

Tests use a fake pi script and shortened timers: exits on stdin EOF, ignores
stdin, ignores SIGTERM, and spawns a sleeping grandchild to verify group kill.

## Replay and resync

Pi already exposes a durable cursor: `get_entries` with `since: <entryId>`
returns entries after that id, including pre-compaction history and abandoned
branches. Use it as the source of truth.

On first attach or a detected gap:

1. Call `get_entries` (optionally with the client's last entry id) and
   `get_state`.
2. Replace the client's message state.
3. Apply live events after the snapshot.

`get_messages` is not the hydration primitive for a tree-capable UI; it hides
pre-compaction entries and branch structure. A small monotonic live `seq` is
still useful to detect gaps between event batches, but it is not the replay
cursor. A small byte-capped event ring can avoid a round trip on short
disconnects; it is an optimization, not the correctness mechanism.

Resync is required after `compact`, `new_session`, `switch_session`, `fork`,
and `clone`, because the message list and active leaf can change. Banners
alone are not enough.

## Wire protocol

Keep the existing text-JSON control plane and binary terminal frames. Add:

- `create` accepts `mode: "pi"` plus `cwd`, `title`, and `trust`.
- Client to server: `{"type":"pi","session_id":...,"command":{...}}`, a raw
  RPC command envelope. Webterm does not model every RPC command.
- Server to client: the RPC response is returned as `pi_response` in the
  correlated `result` for the browser's request id. Server-generated RPC ids
  avoid collisions when two clients drive the same session.
- Server to client: `pi_event`, `{session_id, seq, record}`.
- Server to client: `pi_resync` when a client attaches or a gap is detected.
- Extension UI: `pi_ui_request`, `pi_ui_resolved`, `pi_ui_response`, plus a
  small replayed `ui_state` for status/widget/title.
- `kind`, `closing`, and `trusted` on session metadata.

`input` and binary offset frames remain terminal-only. Sending `input` or
`resize` to a pi session is a request error, not a silent no-op.

## Project trust

Project trust gates project-local settings, extensions, and skills. It is a
code-loading decision made at pi startup, not a sandbox and not a per-tool
approval. `AGENTS.md` and similar context files load regardless of trust.

RPC mode cannot show pi's built-in trust prompt: the decision happens during
resource loading before the RPC loop, and the built-in selector is a TUI
startup screen rather than an `extension_ui_request`. Without a saved decision,
pi silently proceeds untrusted.

Webterm makes the decision explicit before spawning:

- The frontend asks once per launch, showing the exact cwd, with two clear
  choices: start with project settings/extensions, or start without them.
  Remembering the choice in `localStorage` is allowed; it is never pre-checked.
- `create` for a pi session accepts `trust: "allow"` or `"deny"`.
- The server maps `allow` to `--approve` and `deny` to `--no-approve`.
- A missing `trust` means `--no-approve`. There is no implicit approval and
  no trust-store write.
- Trust is accepted only on pi creates, never on shell creates.
- Agent-origin spawns default to `--no-approve` and do not prompt; a parent
  cannot launder approval into a child.
- `trusted` in session info drives an "untrusted project" chip with an
  explicit restart-with-trust action.

Because `--approve` is harmless when a directory has no trust-gated resources,
webterm does not detect or mirror pi's resource list. The trade-off is that a
saved deny in pi's trust store can be overridden by a browser click; the click
is explicit and per-launch, and the UI says what it does.

## Session storage

P0 runs pi with `--no-session`; reconnect only needs the server process to
keep running. Persistence, the resume picker, fork/clone, and export land in a
later phase, using a webterm-managed root:

- Default `$XDG_STATE_HOME/webterm/pi-sessions`, falling back to
  `~/.local/state/webterm/pi-sessions`.
- Override with `--pi-session-dir` or `WEBTERM_PI_SESSION_DIR`.
- Create it `0700`, resolve the real path once at startup, and expose it in
  `/config.js`.
- Pass `--session-dir <root>` to every pi spawn and set
  `PI_CODING_AGENT_SESSION_DIR` in the pi child environment so nested pi
  processes stay in the managed root. Do not set it for shell sessions.

The root deliberately overrides pi's global and project `sessionDir` for
webterm sessions. Sessions from the user's normal pi store are not listed; a
read-only import root can be added later without changing the writable root.

Pi keeps its grouping under the root
(`--<cwd>--/<timestamp>_<id>.jsonl`), so the picker walks recursively and
parses only the first `session` header line per file for the list. Paths from
the browser are resolved with `realpath` and rejected unless they are under the
root. The raw RPC channel is still equivalent to shell access, so this is
accident prevention rather than a security boundary.

Deletion is explicit: no automatic session-file removal by default. Optional
retention flags can remove files by age or count, never a file backing a live
session. Subagent sessions default to `--no-session`; persistence is opt-in.

For TUI interop, offer "Open in TUI" by spawning
`pi --session-dir <root> --resume <file>` in a PTY tab, only when the RPC
session is closed. Verify pi's file locking before allowing two processes on
one session file; otherwise copy the file first.

## Extension dialogs

Dialog state is server-owned, not client-local:

- Queue `extension_ui_request` items per session with the pi request id,
  method, text, options, timeout, and creation time.
- Broadcast `pi_ui_request`, replay the queue on attach, and mark the session
  `needs_input`.
- Accept one valid response per id; write exactly one
  `extension_ui_response` to pi; broadcast `pi_ui_resolved` so other clients
  dismiss it.
- A client disconnecting does not cancel a dialog. Only a response, pi's own
  timeout, or the unattended policy resolves it.
- Do not let browsers send raw `extension_ui_response` through the generic
  command envelope; the server correlates and validates.

Unattended policy:

| Policy | Behavior | Used for |
|---|---|---|
| `wait` | Pending until answered or pi's own timeout | Browser-origin sessions |
| `deny` | Cancel immediately when no client is attached | Agent-origin sessions |
| `auto` | Wait a configurable grace period, then cancel | Optional |

On a webterm timeout, send `cancelled: true` for select/input/editor and
`confirmed: false` for confirm. Post a visible system note; never deny
silently.

Fire-and-forget methods are session state. `notify` is broadcast; `setStatus`,
`setWidget`, `setTitle`, and `setEditorText` keep the latest value per key and
replay on attach. `setEditorText` must not overwrite a non-empty draft.

Multiple viewers share one agent, tmux-style. Any authenticated viewer may
answer; first response wins; there is no owner concept. Dialog text, options,
widgets, and notifications are rendered as text, never through `innerHTML`.

## Deferred decisions

- **Subagents.** Whether they land after P1 or only in P4; webterm-injected
  extension via `-e` versus `bash` + `webtermctl`; whether the parent sees final
  text only or live child events; fan-out and cost caps.
- **Compatibility.** Minimum supported pi version, and whether to gate on
  `pi --version` or feature-detect from `get_state` / `get_commands` and ignore
  unknown events.
- **Persistent trust.** Reading pi's trust store to respect a saved deny, or
  writing a persistent webterm decision.
- **Server restart.** Pi sessions die with the server today; decide whether a
  persisted session file enables resume after restart.
- **Export serving.** `export_html` returns a path; serving it to the browser
  needs the same root validation as the picker.

## P0 scope

Acceptance test: create a pi session, send a prompt, see streamed text and one
tool card, abort, disconnect and reconnect, get the correct transcript, then
kill the session with no orphaned pi or bash children.

In scope:

- `PiSession` with the process, pipe, reaper, and two-phase-close behavior
  above.
- `kind`, `trusted`, and `closing` metadata; `Session | PiSession` typing.
- `create(mode:"pi")`, the raw `pi` command envelope, `pi_event`,
  `pi_response`, and entry-based resync.
- `prompt`, streamed text, `tool_execution_*` cards, `agent_settled`, `abort`.
- The frontend trust choice and the `--approve` / `--no-approve` mapping.
- A minimal chat view and event reducer in `webterm/static/pi.js` (a classic
  script exposing `window.WebtermPi`).
- A fake pi test script covering normal flow, a large record, process death,
  reconnect/resync, and group kill.

Out of scope:

- Model and thinking pickers, queue/steer/follow-up, compaction UI.
- Session persistence, the resume picker, fork/clone/tree, export.
- Extension dialog UI (the server policy above is the design, not P0).
- Subagents and agent-profile changes.
- Markdown rendering, `@file` mentions, mobile polish.

## Security

Pi sessions have the same privileges as the shell webterm already provides:
the agent can read, write, and execute as the server user. Project trust is a
code-loading gate, not a security boundary, and answering a dialog is not
authorization. The default bind stays `127.0.0.1`; remote exposure still
requires the existing token/TLS/Tailscale story. Session files and exported
HTML may contain prompts, tool output, and file contents, and are treated as
sensitive.
