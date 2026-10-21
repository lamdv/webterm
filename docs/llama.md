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
from `/v1/models` is selected on the first prompt. The browser shows a
**llama chat** launcher and empty-state choice for the profile, so a human can
open a root chat tab without the CLI.

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

## Behaviour

The adapter process speaks webterm's canonical JSONL protocol directly on
stdin/stdout, so there is no Python codec and no special handling in
`server.py`:

| Command | Behaviour |
|---|---|
| `prompt` | Appends the user entry, returns `started`, streams from `POST /v1/chat/completions`. |
| `abort` | Closes the in-flight HTTP response; the run finishes with reason `aborted`. |
| `get_state` | Current model, provider, and streaming flag. |
| `get_snapshot` / `get_history` | In-memory canonical transcript, newest-page and backwards paging. |
| `list_models` | `GET /v1/models`, normalized to `{id, provider, name}`; accepts both llama-swap's `data` and llama-server's `models` shapes. |
| `set_model` | Updates the model and emits `session.info`. |
| `raw` | `get_last_assistant_text` and `get_session_stats`, used by `agent_result`. |
| `shutdown` | Aborts any run and exits. |

Prompts send the full text transcript as OpenAI chat messages. The request asks
for `stream: true` plus `stream_options.include_usage`, so usage tokens appear
as a canonical `usage` event. A `reasoning_content` delta becomes a thinking
block; normal `content` deltas become text. Errors surface as a `notice` and
`run.finished` with reason `error`, without killing the session.

Capabilities advertised in `hello` are deliberately minimal: prompt, abort,
models list/set, snapshot, history paging, token usage, and the `raw`
introspection above. Steering, follow-ups, queues, dialogs, tools, images,
resume, and export are off, so the UI hides those controls. The native codec's
spawn result also reads `get_last_assistant_text`, so a parent can collect the
child's answer with `agent_result` or a waiting `webtermctl spawn`.

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
selection, snapshot/history, and abort. `tests/test_control.py` covers profile
adapter inference and native `set_model`; `tests/test_ctl.py` covers pi
`--provider`/`--model` forwarding.
