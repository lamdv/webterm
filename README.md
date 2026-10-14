# webterm

A web-based terminal multiplexer. It runs persistent shell sessions on a server
(a la `tmux`/`screen`) and lets you attach to them from any browser over a
WebSocket.

Because the sessions live in the server process, not the browser tab, you can
close the tab, switch machines, or refresh and your shell is still exactly where
you left it — including full scrollback.

```
┌────────────────────────────────────────────────────┐
│  webterm[+ new] │                                  │
├─────────────────┬──────────────────────────────────┤
│  shell 1        │                                  │
│  shell 2        │   $ npm run dev                  │
│  logs           │   ...                            │
│                 │                                  │
└─────────────────┴──────────────────────────────────┘
```

## Features

- Server-side PTY sessions that survive browser refreshes and disconnects
- Multiple sessions (tabs) managed from a sidebar that can be resized and
  collapsed
- Reattach with scrollback replay
- Multiple browsers can attach to the same session simultaneously
- Full terminal emulation via [xterm.js](https://xtermjs.org/): colors,
  Unicode, mouse reporting, alt-screen apps (vim, htop, ...)
- Resizes propagate to the PTY
- **Zero Python dependencies** — the WebSocket server is built on the standard
  library, and xterm.js is loaded in the browser from a pinned, integrity-checked
  CDN URL, so there is no build step and nothing to install

## Requirements

- Python 3.11+
- A POSIX system (Linux/macOS). Windows is not supported (no `forkpty`).

No third-party Python packages are required. [uv](https://docs.astral.sh/uv/)
handles the virtualenv, but plain `python -m venv` works too.

The browser loads [xterm.js](https://xtermjs.org/) from jsDelivr, so the client
needs network access. To run fully offline, download `xterm.js` and `xterm.css`
and point the two asset tags in `webterm/static/index.html` at local copies.

## Install & run

```bash
uv sync
uv run webterm
```

Or, without uv:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/webterm
```

Then open <http://127.0.0.1:8765>.

By default it binds to `127.0.0.1`. To expose it on your network:

```bash
uv run webterm --host 0.0.0.0 --port 8765
```

> ⚠️ webterm gives whoever can reach it a shell on your machine. There is no
> authentication yet. Only bind to `0.0.0.0` on trusted networks, or put it
> behind a reverse proxy that handles auth/TLS.

## Usage

- **New session** — click `+ new` (or press `Ctrl` `Shift` `N`) to spawn a shell.
- **Launchers** — the sidebar shows a button per `--launcher`, in a horizontally
  scrollable row. The leading **`+`** pill prompts for a command and an optional
  name, runs it in a new shell session, and keeps it as a pill (saved in
  `localStorage`) for reuse. Remove a user pill with its `×`, or by long-pressing
  it (touch) / right-clicking it (desktop). Launched commands are typed into a
  new shell session, so if they exit (or aren't installed) you land back at a
  prompt rather than losing the tab. By default, if `pi` is on `PATH`, a **pi
  agent** button starts `pi`. Add your own with e.g.
  `--launcher "btop=btop" --launcher "logs=journalctl -f"`.
- **Switch session** — click a tab in the sidebar.
- **Resize sidebar** — drag the divider between the sidebar and the terminal
  (or focus it and use `←`/`→`, `Home` to reset).
- **Collapse sidebar** — click the `‹` button, press `Ctrl` `Shift` `B`, or
  double-click the divider to reset its width. The sidebar width and collapsed
  state are remembered in `localStorage`.
- **Close session** — click the `×` on a tab; this kills the shell.
- **Rename** — double-click a tab title.
- **Reconnect** — just reload the page; sessions stay alive until the server
  exits or you close them.

## Configuration

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Interface to bind |
| `--port` | `8765` | Port to listen on |
| `--shell` | `$SHELL` or `/bin/bash` | Shell to launch |
| `--font` | Nerd Font aware stack | CSS `font-family` for the browser terminal |
| `--launcher` | `pi agent` (if `pi` is installed) | Add a button that starts a session running `LABEL=COMMAND` (repeatable) |
| `--cwd` | `$HOME` | Working directory for new sessions |
| `--scrollback` | `100000` | Bytes of scrollback kept per session for reattach |

## How it works

```
browser (xterm.js)  ──WebSocket(JSON + length-prefixed bytes)──▶  asyncio server
                                                                    │
                                                                    ├─ SessionManager
                                                                    │    └─ Session ── PTY master ── shell
                                                                    │         └─ ring buffer (replay)
                                                                    └─ static files (index.html, app.js)
```

Each `Session` owns a PTY master fd and a child process. The event loop reads
the PTY and fans output out to every attached WebSocket while appending to a
bounded scrollback buffer. When a browser attaches, the buffer is replayed
first. Terminal output is sent as binary frames prefixed with a one-byte
session-id length so a single socket can carry several sessions at once;
control messages (create, kill, rename, resize, ...) are JSON text frames.
Children are reaped through a `SIGCHLD` handler in the `SessionManager`.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run python -m unittest discover -s tests -t . -v
```

## License

MIT

No third-party code is bundled in this repository. The client loads
[xterm.js](https://xtermjs.org/) from a CDN at runtime.
