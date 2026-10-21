# llama-server / llama-swap chat agent

Webterm can expose a local llama-server or llama-swap model as a structured
agent session. It is a **chat** adapter: prompts stream into webterm's normal
agent view, with the same tabs, tree, replay, and remote access as any other
session. It has no tools, shell access, file edits, or autonomous loop — use pi
with llama-swap as its provider when you want an agentic worker.

Status: **implemented** as a native canonical adapter
(`webterm/integration/llama/`).

## Enabling it

Pass the base URL, or set the environment variable:

```bash
uv run webterm --llama-url http://127.0.0.1:8080
# equivalent
WEBTERM_LLAMA_URL=http://127.0.0.1:8080 uv run webterm
```

Use `http://`. Neither llama-swap nor llama-server terminates TLS, so an
`https://` base URL fails the handshake (`[SSL] record layer failure`) unless a
TLS proxy such as Caddy fronts the port; the adapter detects that case and says
so in the error instead of leaving the bare OpenSSL message.

`--llama-url` adds a `llama` agent profile with `adapter: "llama"` and an `rpc`
command that runs the adapter in the webterm interpreter. `--llama-model` (or
`$WEBTERM_LLAMA_MODEL`) sets its default model; without one, the first model
from `/v1/models` is selected on the first prompt. `--llama-system-prompt` and
`--llama-thinking` (or `$WEBTERM_LLAMA_SYSTEM_PROMPT` and
`$WEBTERM_LLAMA_THINKING`) set the initial system prompt and thinking level.
The browser shows a **llama chat** launcher and empty-state choice for the
profile, so a human can open a root chat tab without the CLI.

The base URL points at either service:

- **llama-swap** (typically `http://127.0.0.1:8080`): `/v1/models` lists one
  entry per configured model, and a request swaps to that model.
- **llama-server** directly (its own port): `/v1/models` lists the loaded
  model. The same OpenAI-compatible endpoints are used.

## Starting a session

From a terminal:

```bash
webtermctl spawn --agent llama --rpc --model gemma4-26b \
  --task "Write the opening of a ghost story." --no-wait
```

From inside a pi session, the bundled extension exposes the same profile:

```text
spawn_agent(task="Write ...", agent="llama", model="gemma4-26b")
```

`--wait` (or `agent_wait`) waits for `run.settled`; `agent_result` reads the
last assistant text. The browser's model picker is populated by
`list_models`/`set_model`, so a running chat can switch models without a new
session.

Model selection for a spawn is generic: `spawn` accepts `provider` and `model`
for any structured (`rpc`) child. pi receives them as `--provider`/`--model`;
a native adapter is sent the canonical `set_model` command before its first
prompt. Selection without `rpc` is rejected rather than silently ignored.

## Chat controls

The canonical agent view gives a llama session three controls above the
transcript; each sends one canonical command and applies to the next reply:

- **model** — populated from `list_models`; selecting sends `set_model`. The
  llama-swap router swaps models on the next request.
- **thinking** — the levels the adapter advertises
  (`default`, `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`).
  Selecting sends `set_thinking`; `default` leaves the server's own
  `--reasoning-effort` setting alone.
- **system prompt** — a panel with Save/Clear sends `set_system_prompt`. The
  text is prepended as a `system` message to every request and is never part
  of the visible transcript.

Thinking levels map onto llama-server's request fields:

| Level | Request fields |
|---|---|
| `default` | none — the server and chat template decide |
| `off` | `"reasoning_effort": "none"` plus `chat_template_kwargs:{"enable_thinking":false}` |
| any other level | `"reasoning_effort":"<level>"` plus `chat_template_kwargs:{"enable_thinking":true,"reasoning_effort":"<level>"}` |

Recent llama-server builds read `reasoning_effort` directly; older ones and
most GGUF chat templates read `enable_thinking` from `chat_template_kwargs`.
Sending both keeps one adapter working across builds. Templates that do not
support thinking ignore the fields, so the control is harmless for models such
as Gemma.

## Prompt library

Reusable system prompts live as plain-text files, one per prompt, so a prompt
does not have to be retyped for every session. The adapter resolves one
directory, in this order:

1. `--prompt-dir DIR` on the adapter process;
2. `$WEBTERM_PROMPT_DIR`;
3. `$XDG_CONFIG_HOME/webterm/prompts`;
4. `~/.config/webterm/prompts`.

The adapter is started by webterm, not by hand, so a persistent choice is
usually `WEBTERM_PROMPT_DIR` in the server's environment (the flag exists for
direct runs and tests). Rules:

- one `<name>.md` file per prompt, UTF-8 plain text, directly in the directory
  (no subdirectories);
- names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`: no leading dot, no `/`, no
  `..`, so a name can never address a path outside the directory;
- the text is stripped, must be non-empty, and is limited to 16384 characters;
- the first use of a *missing* directory creates it and seeds four starters
  (`concise`, `code-review`, `explain`, `socratic`, one to three lines each).
  Existing files are never overwritten, and a directory that already exists is
  not seeded, even when it is empty;
- listing is sorted by name and skips any file that is not a valid prompt
  (wrong extension or name, unreadable, empty, or too long) instead of failing.

The library is served by three canonical commands:

| Command | Fields | Response `data` |
|---|---|---|
| `list_prompts` | — | `{"prompts":[{"name":str,"text":str}, ...]}`, sorted by name |
| `save_prompt` | `name`, `text` | `{"prompt":{"name":str,"text":str}}`; `text` is the normalized (stripped) text |
| `delete_prompt` | `name` | `{"name":str,"deleted":bool}`; deleting a missing prompt answers `deleted:false`, not an error |

`hello` advertises `"prompt_library": true` so a client can hide the controls
for adapters without a library. A rejected name or text is an ordinary
`success:false` response with a human-readable `error`, so the session keeps
running.

The library is storage only. Saving or deleting a prompt never changes the
session's active system prompt: applying one is a separate
`set_system_prompt` from the client. The library also knows nothing about the
transcript; it is not part of the conversation, usage, or history.

## Behaviour

The adapter process speaks webterm's canonical JSONL protocol directly on
stdin/stdout, so there is no Python codec and no special handling in
`server.py`:

| Command | Behaviour |
|---|---|
| `prompt` | Appends the user entry, returns `started`, streams from `POST /v1/chat/completions`. |
| `abort` | Closes the in-flight HTTP response; the run finishes with reason `aborted`. |
| `get_state` | Current model, provider, streaming flag, thinking level, and system prompt. |
| `get_snapshot` / `get_history` | In-memory canonical transcript, newest-page and backwards paging. |
| `list_models` | `GET /v1/models`, normalized to `{id, provider, name}`; accepts both llama-swap's `data` and llama-server's `models` shapes. |
| `set_model` | Updates the model and emits `session.info`. |
| `set_thinking` | Updates the thinking level, emits `session.info`, and validates it against the advertised levels. |
| `set_system_prompt` | Updates the system prompt, emits `session.info`, and sends it as the first `system` message on later requests. |
| `list_prompts` / `save_prompt` / `delete_prompt` | The plain-text prompt library (see above); saving a prompt does not touch the active system prompt. |
| `raw` | `get_last_assistant_text` and `get_session_stats`, used by `agent_result`. |
| `shutdown` | Aborts any run and exits. |

Prompts send the full text transcript as OpenAI chat messages. The request asks
for `stream: true` plus `stream_options.include_usage`, so usage tokens appear
as a canonical `usage` event. A `reasoning_content` delta becomes a thinking
block; normal `content` deltas become text. Errors surface as a `notice` and
`run.finished` with reason `error`, without killing the session.

Capabilities advertised in `hello` are deliberately minimal: prompt, abort,
models list/set with thinking levels, a system prompt, a prompt library,
snapshot, history paging, token usage, and the `raw` introspection above.
Steering, follow-ups, queues, dialogs, tools, images, resume, and export are
off, so the UI hides those controls. The native codec's spawn result also reads
`get_last_assistant_text`, so a parent can collect the child's answer with
`agent_result` or a waiting `webtermctl spawn`.

There is no session file: the transcript lives in the adapter process for the
lifetime of the session. Restarting the session starts a new conversation.

## Relationship to llama-swap's `/ui`

llama-swap's own SPA (typically `/ui/`) is a separate client for the same
`/v1/chat/completions` endpoint. Webterm does not embed or proxy it. The
adapter exists so a llama model can be a first-class session in webterm's
tree and control plane; if you specifically want llama-swap's sampler and
model-management playground, open `/ui/` directly in a browser.

## Testing

`tests/test_llama_agent.py` runs a small OpenAI-compatible streaming server and
drives a real `AgentSession`: streaming text, usage, model discovery and
selection, the system prompt and thinking fields on the request, validation,
snapshot/history, abort, and the prompt library (seeding, save, list, delete,
name/size validation, and the `--prompt-dir`/environment override).
`tests/test_control.py` covers profile adapter inference and native
`set_model`; `tests/test_ctl.py` covers pi `--provider`/`--model` forwarding.
`tests/agent_view_test.js` exercises the model, thinking, system-prompt, and
prompt-library controls in the canonical browser view.
