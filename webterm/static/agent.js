/* Canonical agent chat view.  Plain script, no build step: WebtermAgent.
 *
 * This is the browser-side consumer of the shadow agent_* stream.  It is
 * deliberately separate from pi.js so the existing pi view remains the default
 * while the canonical model is validated.  Enable it with ?agentui=1.
 */
"use strict";

(function () {
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function textFromContent(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    const parts = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text" && typeof block.text === "string") parts.push(block.text);
      else if (block.type === "thinking" && typeof block.text === "string") parts.push(block.text);
    }
    return parts.join("");
  }

  function messageText(message) {
    if (!message || typeof message !== "object") return "";
    return textFromContent(message.content);
  }

  const HIGHLIGHT = window.WebtermHighlight || null;

  /** Entries requested per backwards page, matching the pi view. */
  const HISTORY_PAGE_SIZE = 40;

  function renderPlain(parent, text) {
    parent.appendChild(el("div", "pi-text", String(text || "")));
  }

  function renderText(parent, text) {
    if (HIGHLIGHT) HIGHLIGHT.renderText(parent, text);
    else renderPlain(parent, text);
  }

  function renderAnswer(parent, text) {
    if (HIGHLIGHT && HIGHLIGHT.renderMarkdown) HIGHLIGHT.renderMarkdown(parent, text);
    else renderText(parent, text);
  }

  function renderPre(parent, text, className) {
    parent.appendChild(el("pre", className || "agent-pre", String(text || "")));
  }

  function roleLabel(entry) {
    if (entry.role === "user") return "you";
    if (entry.role === "assistant") return "assistant";
    if (entry.role === "tool") return entry.tool_name || "tool";
    if (entry.role === "system") return "system";
    return entry.role || "message";
  }

  function create(session, options) {
    options = options || {};
    const onPrompt =
      typeof options.onPrompt === "function"
        ? options.onPrompt
        : () => Promise.resolve();
    const onAbort =
      typeof options.onAbort === "function" ? options.onAbort : () => Promise.resolve();
    const onDialogResponse =
      typeof options.onDialogResponse === "function"
        ? options.onDialogResponse
        : () => Promise.resolve();
    const onHistory =
      typeof options.onHistory === "function" ? options.onHistory : null;
    const root = el("div", "pi-view agent-view");
    const header = el("div", "pi-header agent-header");
    const titleEl = el("span", "pi-title", session.title || "agent");
    const metaEl = el("span", "pi-status agent-header-meta", "");
    header.append(titleEl, metaEl);

    const banner = el("div", "pi-banner agent-banner");
    banner.hidden = true;
    const messagesEl = el("div", "pi-messages agent-messages");
    const dialogHost = el("div", "agent-dialog-host");
    dialogHost.hidden = true;

    const footer = el("div", "pi-footer agent-footer");
    const statusEl = el("span", "pi-status", "starting…");
    footer.append(statusEl);

    const form = el("form", "pi-composer agent-composer");
    const input = el("textarea", "pi-input", "");
    input.rows = 2;
    input.placeholder =
      "Message the agent… (Enter sends/queues, Ctrl+Enter steers while busy, Shift+Enter adds a line)";
    input.setAttribute("aria-label", "Message the agent");
    const actions = el("div", "pi-composer-actions");
    const sendButton = el("button", "pi-send", "Send");
    sendButton.type = "submit";
    const stopButton = el("button", "agent-stop", "Stop");
    stopButton.type = "button";
    actions.append(sendButton, stopButton);
    form.append(input, actions);

    root.append(header, banner, messagesEl, dialogHost, form, footer);

    const state = {
      entries: [],
      live: null,
      cursor: -1,
      busy: false,
      settled: true,
      exited: false,
      connected: false,
      meta: {},
      capabilities: {},
      usage: { tokens: 0, cost: 0 },
      toolState: new Map(),
      dialogs: new Map(),
      history: {
        hasMore: false,
        firstId: null,
        loading: false,
        error: "",
        generation: 0,
      },
      optimisticCounter: 0,
      localCounter: 0,
      noticeTimer: null,
    };

    let stickToBottom = true;
    let renderQueued = false;
    let scrollAnchor = null;
    messagesEl.addEventListener("scroll", () => {
      const distance =
        messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
      stickToBottom = distance < 40;
    });

    function textOfEntry(entry) {
      return textFromContent(entry && entry.content);
    }

    function showNotice(text, level) {
      banner.textContent = String(text || "");
      banner.className = "pi-banner agent-banner" + (level ? " " + level : "");
      banner.hidden = false;
      if (state.noticeTimer) clearTimeout(state.noticeTimer);
      state.noticeTimer = setTimeout(() => {
        banner.hidden = true;
      }, 5000);
    }

    function updateStatus() {
      const bits = [];
      if (state.exited) bits.push("exited");
      else if (state.busy) bits.push("working…");
      else if (state.meta.streaming) bits.push("working…");
      else if (!state.connected) bits.push("waiting for stream…");
      else bits.push("ready");
      const model = state.meta.model || session.model;
      if (model) bits.push(String(model));
      if (state.usage.tokens) bits.push(state.usage.tokens + " tok");
      if (state.usage.cost > 0) bits.push("$" + Number(state.usage.cost).toFixed(4));
      statusEl.textContent = bits.join(" · ");
      statusEl.classList.toggle("busy", state.busy);
      statusEl.classList.toggle("exited", state.exited);

      const canPrompt = !state.exited && state.capabilities.prompt !== false;
      input.disabled = !canPrompt;
      sendButton.disabled = !canPrompt;
      stopButton.hidden = !state.busy || state.capabilities.abort === false;
      metaEl.textContent = model ? String(model) : "";
    }

    function blockContent(block, messageRole) {
      const wrap = el("div", "agent-block");
      if (!block || typeof block !== "object") {
        renderPlain(wrap, String(block || ""));
        return wrap;
      }
      if (block.type === "text") {
        if (messageRole === "assistant") renderAnswer(wrap, block.text || "");
        else renderText(wrap, block.text || "");
        return wrap;
      }
      if (block.type === "thinking") {
        const details = el("details", "agent-thinking");
        const summary = el("summary", "", "Thinking");
        renderPre(details, block.text || "");
        details.prepend(summary);
        wrap.appendChild(details);
        return wrap;
      }
      if (block.type === "tool_call") {
        const card = el("div", "agent-tool");
        const head = el("div", "agent-tool-head");
        head.append(el("span", "agent-tool-name", block.name || "tool"));
        const status = state.toolState.get(block.call_id);
        if (status) {
          const chip = el(
            "span",
            "agent-tool-status" + (status.isError ? " error" : status.done ? " ok" : ""),
            status.done ? (status.isError ? "error" : "done") : "running…"
          );
          head.append(chip);
        }
        card.append(head);
        if (block.args && Object.keys(block.args).length) {
          renderPre(card, JSON.stringify(block.args, null, 2), "agent-tool-args");
        } else if (block.arguments_text) {
          renderPre(card, block.arguments_text, "agent-tool-args");
        }
        if (status && status.output) {
          renderPre(card, status.output, "agent-tool-output");
        }
        wrap.appendChild(card);
        return wrap;
      }
      if (block.type === "image") {
        wrap.appendChild(el("div", "agent-image", "[image " + (block.mime_type || "") + "]"));
        return wrap;
      }
      if (block.type === "file") {
        wrap.appendChild(el("div", "agent-file", "[file]"));
        return wrap;
      }
      renderPre(wrap, JSON.stringify(block, null, 2), "agent-pre");
      return wrap;
    }

    function renderMessage(entry, isLive) {
      const article = el("article", "pi-msg agent-msg");
      if (entry.role) article.classList.add("role-" + entry.role);
      if (entry.role === "tool") {
        article.classList.add(entry.is_error ? "error" : "ok");
      }
      if (entry.optimistic) article.classList.add("optimistic");
      if (isLive) article.classList.add("live");
      const role = el("div", "pi-role", roleLabel(entry));
      article.append(role);
      const blocks = Array.isArray(entry.content) ? entry.content : [];
      if (entry.role === "tool") {
        renderPre(article, textOfEntry(entry), "agent-tool-output");
      } else if (!blocks.length) {
        renderPlain(article, isLive ? "…" : "");
      } else {
        for (const block of blocks) article.append(blockContent(block, entry.role));
      }
      return article;
    }

    function renderDialogs() {
      dialogHost.innerHTML = "";
      dialogHost.hidden = state.dialogs.size === 0;
      for (const dialog of state.dialogs.values()) {
        const panel = el("div", "agent-dialog");
        const title = dialog.title || (dialog.kind === "confirm" ? "Confirm" : "Input needed");
        panel.append(el("div", "agent-dialog-title", title));
        if (dialog.message) panel.append(el("div", "agent-dialog-message", dialog.message));
        const actionsEl = el("div", "agent-dialog-actions");

        const respond = (response) => {
          for (const button of actionsEl.querySelectorAll("button")) button.disabled = true;
          const promise = onDialogResponse(dialog.id, dialog.kind, response);
          if (promise && promise.catch) {
            promise.catch((error) => {
              showNotice(error && error.message ? error.message : "dialog failed", "error");
              for (const button of actionsEl.querySelectorAll("button")) button.disabled = false;
            });
          }
        };

        if (dialog.kind === "confirm") {
          const yes = el("button", "agent-dialog-primary", "Approve");
          yes.type = "button";
          yes.addEventListener("click", () => respond({ approved: true }));
          const no = el("button", "", "Deny");
          no.type = "button";
          no.addEventListener("click", () => respond({ approved: false }));
          actionsEl.append(yes, no);
        } else if (dialog.kind === "select") {
          const choices = Array.isArray(dialog.options) ? dialog.options : [];
          for (const choice of choices) {
            const button = el("button", "", String(choice));
            button.type = "button";
            button.addEventListener("click", () => respond({ value: choice }));
            actionsEl.append(button);
          }
        } else {
          const field = el(dialog.kind === "editor" ? "textarea" : "input", "agent-dialog-input");
          if (dialog.kind === "editor") field.rows = 5;
          if (dialog.placeholder) field.placeholder = dialog.placeholder;
          if (dialog.prefill) field.value = dialog.prefill;
          panel.append(field);
          const submit = el("button", "agent-dialog-primary", "Submit");
          submit.type = "button";
          submit.addEventListener("click", () => respond({ value: field.value }));
          actionsEl.append(submit);
        }

        const cancel = el("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => respond({ cancelled: true }));
        actionsEl.append(cancel);
        panel.append(actionsEl);
        dialogHost.append(panel);
      }
    }

    function historyRow() {
      const history = state.history;
      const row = el("div", "pi-history");
      if (history.loading) {
        row.append(el("span", "pi-history-status", "Loading earlier messages…"));
      } else if (history.hasMore) {
        const button = el("button", "pi-history-more", "Load earlier messages");
        button.type = "button";
        button.disabled = state.exited;
        button.addEventListener("click", loadEarlier);
        row.append(button);
      }
      if (history.error) row.append(el("span", "pi-history-error", history.error));
      return row;
    }

    function loadEarlier() {
      const history = state.history;
      if (state.exited || history.loading || !history.hasMore || !history.firstId) return;
      if (!onHistory) {
        showNotice("earlier messages are unavailable", "warning");
        scheduleRender();
        return;
      }
      const before = history.firstId;
      const generation = history.generation;
      history.loading = true;
      history.error = "";
      scrollAnchor = { height: messagesEl.scrollHeight, top: messagesEl.scrollTop };
      render();
      Promise.resolve(onHistory(before, HISTORY_PAGE_SIZE))
        .then((page) => {
          if (generation !== history.generation) return;
          const entries = page && Array.isArray(page.entries) ? page.entries : [];
          const known = new Set(state.entries.map((entry) => entry.id).filter(Boolean));
          const older = entries.filter((entry) => entry && !known.has(entry.id));
          history.loading = false;
          history.hasMore = Boolean(page && page.has_more);
          if (page && typeof page.first_entry_id === "string") {
            history.firstId = page.first_entry_id;
          } else if (!older.length) {
            history.hasMore = false;
          }
          state.entries = older.concat(state.entries);
          render();
        })
        .catch((error) => {
          if (generation !== history.generation) return;
          history.loading = false;
          history.error =
            error && error.message ? error.message : "could not load earlier messages";
          render();
        });
    }

    function render() {
      const atBottom = stickToBottom;
      const anchor = scrollAnchor;
      const previousTop = messagesEl.scrollTop;
      scrollAnchor = null;
      messagesEl.innerHTML = "";
      if (state.history.hasMore || state.history.loading || state.history.error) {
        messagesEl.append(historyRow());
      }
      if (!state.entries.length && !state.live) {
        messagesEl.append(el("div", "pi-empty", "No messages yet."));
      } else {
        for (const entry of state.entries) messagesEl.append(renderMessage(entry, false));
        if (state.live) messagesEl.append(renderMessage(state.live, true));
      }
      renderDialogs();
      updateStatus();
      if (atBottom) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else if (anchor) {
        // Keep the reader's place when an earlier page is prepended.
        messagesEl.scrollTop = anchor.top + (messagesEl.scrollHeight - anchor.height);
      } else {
        messagesEl.scrollTop = previousTop;
      }
      stickToBottom = atBottom;
    }

    function scheduleRender() {
      if (renderQueued) return;
      renderQueued = true;
      requestAnimationFrame(() => {
        renderQueued = false;
        render();
      });
    }

    function upsertMessage(message) {
      if (!message || typeof message !== "object") return;
      const id = message.id || "live-" + ++state.localCounter;
      if (message.role === "user") {
        const text = messageText(message);
        state.entries = state.entries.filter(
          (entry) => !(entry.optimistic && textOfEntry(entry) === text)
        );
      }
      const index = state.entries.findIndex((entry) => entry.id === id);
      if (index >= 0) state.entries[index] = message;
      else state.entries.push(message);
    }

    function ensureLive(messageId) {
      if (!state.live || state.live.role !== "assistant") {
        state.live = {
          id: messageId || "live-" + ++state.localCounter,
          role: "assistant",
          content: [],
        };
      } else if (messageId && state.live.id !== messageId) {
        state.live.id = messageId;
      }
      return state.live;
    }

    function ensureBlock(index, type) {
      const live = ensureLive();
      const at = typeof index === "number" ? index : 0;
      while (live.content.length <= at) live.content.push({ type: type, text: "" });
      let block = live.content[at];
      if (!block || block.type !== type) {
        block = { type: type, text: "" };
        if (type === "tool_call") {
          block = { type: "tool_call", call_id: "", name: "", args: {}, arguments_text: "" };
        }
        live.content[at] = block;
      }
      return block;
    }

    function applyEvent(event) {
      if (!event || typeof event !== "object") return;
      const name = event.event;
      switch (name) {
        case "run.started":
          state.busy = true;
          state.settled = false;
          break;
        case "run.finished":
          state.busy = false;
          break;
        case "run.settled":
          state.busy = false;
          state.settled = true;
          break;
        case "message.started": {
          const message = event.message;
          if (message && message.role === "assistant") {
            state.live = {
              id: message.id || "live-" + ++state.localCounter,
              role: "assistant",
              content: Array.isArray(message.content) ? message.content.slice() : [],
            };
          }
          break;
        }
        case "message.delta": {
          const block = ensureBlock(event.block_index, event.block_type);
          if (event.block_type === "tool_call") {
            block.arguments_text = (block.arguments_text || "") + (event.delta || "");
          } else {
            block.text = (block.text || "") + (event.delta || "");
          }
          break;
        }
        case "message.block": {
          const live = ensureLive();
          const at = typeof event.block_index === "number" ? event.block_index : 0;
          while (live.content.length <= at) live.content.push({ type: "text", text: "" });
          live.content[at] = event.block || { type: "text", text: "" };
          break;
        }
        case "message.finished": {
          const message = event.message;
          if (!message) break;
          upsertMessage(message);
          if (message.role === "assistant") state.live = null;
          break;
        }
        case "tool.started": {
          state.toolState.set(event.call_id, {
            name: event.name,
            args: event.args || {},
            output: "",
            done: false,
            isError: false,
          });
          break;
        }
        case "tool.updated": {
          const tool = state.toolState.get(event.call_id);
          if (tool) tool.output += event.delta || event.output || "";
          break;
        }
        case "tool.finished": {
          const tool = state.toolState.get(event.call_id) || { output: "" };
          tool.output = event.output || tool.output || "";
          tool.done = true;
          tool.isError = Boolean(event.is_error);
          state.toolState.set(event.call_id, tool);
          break;
        }
        case "usage":
          state.usage.tokens = Number(event.total_tokens) || 0;
          state.usage.cost = Number(event.cost) || 0;
          break;
        case "session.info":
          if (event.model) state.meta.model = event.model;
          if (event.name !== undefined) state.meta.name = event.name;
          if (event.title) titleEl.textContent = event.title;
          break;
        case "notice":
          showNotice(event.text || event.code || "notice", event.level);
          break;
        case "queue.updated":
          break;
        default:
          break;
      }
      scheduleRender();
    }

    function resetFromSnapshot(message) {
      state.connected = true;
      state.entries = Array.isArray(message.entries) ? message.entries.slice() : [];
      state.cursor =
        message.cursor && typeof message.cursor.seq === "number"
          ? message.cursor.seq
          : -1;
      state.meta = message.state && typeof message.state === "object" ? message.state : {};
      state.capabilities =
        message.capabilities && typeof message.capabilities === "object"
          ? message.capabilities
          : {};
      state.busy = Boolean(state.meta.streaming);
      state.settled = message.state ? message.state.settled !== false : true;
      state.live = null;
      state.toolState.clear();
      state.dialogs.clear();
      const dialogs = Array.isArray(message.dialogs) ? message.dialogs : [];
      for (const dialog of dialogs) {
        if (dialog && dialog.id) state.dialogs.set(dialog.id, dialog);
      }
      state.usage.tokens = Number(state.meta.tokens) || 0;
      state.usage.cost = Number(state.meta.cost) || 0;
      state.history.generation += 1;
      state.history.loading = false;
      state.history.error = "";
      state.history.hasMore = Boolean(message.has_more);
      state.history.firstId =
        typeof message.first_entry_id === "string" ? message.first_entry_id : null;
      if (state.meta.name) titleEl.textContent = state.meta.name;
      render();
    }

    function handle(message) {
      if (!message || !message.type) return;
      if (message.type === "agent_resync") {
        resetFromSnapshot(message);
      } else if (message.type === "agent_event") {
        applyEvent(message.event);
      } else if (message.type === "agent_dialog_request") {
        const request = message.request;
        if (request && request.id) {
          state.dialogs.set(request.id, request);
          scheduleRender();
        }
      } else if (message.type === "agent_dialog_resolved") {
        state.dialogs.delete(message.dialog_id);
        scheduleRender();
      }
    }

    function sendPrompt(behavior) {
      const text = input.value.trim();
      if (!text || state.exited) return;
      input.value = "";
      // A prompt sent during a run must say how to queue it.  The canonical
      // view asks for follow-up by default; Ctrl+Enter requests steering,
      // matching the pi view's gesture.
      const streamingBehavior = state.busy ? behavior || "followUp" : undefined;
      state.entries.push({
        id: "local-" + ++state.localCounter,
        role: "user",
        content: [{ type: "text", text: text }],
        optimistic: true,
      });
      stickToBottom = true;
      scheduleRender();
      const promise = onPrompt(text, streamingBehavior);
      if (promise && promise.catch) {
        promise.catch((error) => {
          state.entries = state.entries.filter(
            (entry) => !(entry.optimistic && textOfEntry(entry) === text)
          );
          showNotice(error && error.message ? error.message : "prompt failed", "error");
          scheduleRender();
        });
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendPrompt();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      sendPrompt(event.ctrlKey || event.metaKey ? "steer" : undefined);
    });
    stopButton.addEventListener("click", () => {
      const promise = onAbort();
      if (promise && promise.catch) {
        promise.catch((error) =>
          showNotice(error && error.message ? error.message : "abort failed", "error")
        );
      }
    });

    render();
    const streamTimer = setTimeout(() => {
      if (!state.connected && !state.exited) {
        showNotice(
          "No canonical agent stream received. The server may need a restart.",
          "warning"
        );
      }
    }, 5000);
    if (streamTimer && typeof streamTimer.unref === "function") streamTimer.unref();

    return {
      element: root,
      handle,
      setExited: () => {
        state.exited = true;
        state.busy = false;
        updateStatus();
        input.disabled = true;
        sendButton.disabled = true;
        scheduleRender();
      },
      activate: () => {
        if (!state.exited) input.focus();
      },
      destroy: () => {
        clearTimeout(streamTimer);
        if (state.noticeTimer) clearTimeout(state.noticeTimer);
        root.remove();
      },
    };
  }

  window.WebtermAgent = { create };
})();
