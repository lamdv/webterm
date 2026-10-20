# webterm documentation

- [Architecture](architecture.md) — components, data flow, wire protocol,
  concurrency, security, testing.
- [Backend](backend.md) — Python modules, CLI, WebSocket layer, session
  management, command reference.
- [Frontend](frontend.md) — static assets, `app.js` structure, reconnect,
  terminal fitting, launchers, `localStorage`.
- [Agent protocol](agent-protocol.md) — the agent-agnostic JSONL protocol and
  `AgentSession`: capabilities, events, snapshots, dialogs, lifecycle, and the
  adapter path for pi and MVA.
- [pi GUI mode](pi.md) — design decisions for pi as a structured session kind:
  RPC process model, entry-based resync, explicit project trust, managed
  session storage, extension dialogs, and the P0 slice.
- [Remote access](remote-access.md) — exposing webterm safely behind Tailscale
  or an OAuth2 reverse proxy (oauth2-proxy, nginx, Caddy).
- [Sub-agents](agent-control.md) — proposal for a local Unix-socket control API,
  CLI, and MCP tools so an agent can spawn visible sub-agents (lineage, tree
  sidebar, recursion limits) and collect their results (draft for discussion).
