/* Structured pi chat view.  Plain script, no build step: exposes WebtermPi. */
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
      else if (block.type === "image") parts.push("[image]");
    }
    return parts.join("");
  }

  function messageText(message) {
    if (!message || typeof message !== "object") return "";
    if (message.role === "toolResult") return textFromContent(message.content);
    return textFromContent(message.content);
  }

  const HIGHLIGHT = window.WebtermHighlight || null;

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

  function renderCode(parent, text, lang, options) {
    if (HIGHLIGHT) HIGHLIGHT.renderCode(parent, text, lang, options);
    else parent.appendChild(el("pre", "pi-pre", String(text || "")));
  }

  function renderOutput(parent, text, options) {
    if (HIGHLIGHT) HIGHLIGHT.renderOutput(parent, text, options);
    else parent.appendChild(el("pre", "pi-pre", String(text || "")));
  }

  const BUILTIN_COMMANDS = [
    { name: "new", description: "Start a new session", source: "webterm" },
    { name: "compact", description: "Compact the conversation", source: "webterm" },
    { name: "model", description: "Switch model", source: "webterm" },
    { name: "provider", description: "Switch provider", source: "webterm" },
    { name: "thinking", description: "Set the thinking level", source: "webterm" },
    { name: "reload", description: "Reload state and commands", source: "webterm" },
    { name: "fork", description: "Branch from an earlier message", source: "webterm" },
    { name: "clone", description: "Clone the active branch", source: "webterm" },
    { name: "tree", description: "Show the session tree", source: "webterm" },
    { name: "export", description: "Download HTML or JSONL", source: "webterm" },
  ];
  const BUILTIN_NAMES = new Set(BUILTIN_COMMANDS.map((command) => command.name));
  //: Entries requested per backwards transcript page.  The server caps this.
  const HISTORY_PAGE_SIZE = 100;

  function create(session, options) {
    const root = el("div", "pi-view");
    const header = el("div", "pi-header");
    const titleEl = el("span", "pi-title", session.title || "pi");
    const statusEl = el("span", "pi-status", "starting…");
    const uiStatusesEl = el("div", "pi-ui-statuses");
    header.append(titleEl, statusEl, uiStatusesEl);

    const banner = el("div", "pi-banner");
    banner.hidden = true;
    const messagesEl = el("div", "pi-messages");
    let stickToBottom = true;
    messagesEl.addEventListener("scroll", () => {
      const distance =
        messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
      stickToBottom = distance < 40;
    });
    const queueEl = el("div", "pi-queue");
    queueEl.hidden = true;
    const paletteEl = el("div", "pi-command-palette");
    paletteEl.hidden = true;
    const widgetsAboveEl = el("div", "pi-ui-widgets above");
    const widgetsBelowEl = el("div", "pi-ui-widgets below");
    const toastsEl = el("div", "pi-toasts");

    const form = el("form", "pi-composer");
    const input = el("textarea", "pi-input");
    input.rows = 2;
    input.placeholder = "Message pi… (Enter to send, Shift+Enter for a newline)";
    input.setAttribute("aria-label", "Message pi");
    const actions = el("div", "pi-composer-actions");
    const stopButton = el("button", "pi-stop", "Stop");
    stopButton.type = "button";
    const sendButton = el("button", "pi-send");
    sendButton.type = "submit";
    const sendLabel = el("span", "pi-send-label", "Send");
    const sendHint = el("span", "pi-send-hint");
    sendButton.append(sendLabel, sendHint);
    actions.append(sendButton, stopButton);
    form.append(input, actions);

    root.append(
      header,
      banner,
      messagesEl,
      queueEl,
      paletteEl,
      widgetsAboveEl,
      form,
      widgetsBelowEl,
      toastsEl
    );

    const state = {
      seq: -1,
      messages: [],
      live: null,
      liveTools: new Map(),
      queues: { steering: [], followUp: [] },
      piCommands: [],
      paletteItems: [],
      paletteIndex: 0,
      state: {},
      busy: false,
      exited: false,
      banner: "",
      error: "",
      history: {
        hasMore: false,
        firstId: null,
        loading: false,
        error: "",
        generation: 0,
      },
      ui: {
        requests: [],
        status: {},
        widgets: {},
        title: "",
      },
    };

    function setBanner(text) {
      state.banner = text || "";
      banner.textContent = state.banner;
      banner.hidden = !state.banner;
    }

    function renderHeader() {
      const info = state.state || {};
      const model = info.model && (info.model.id || info.model.name);
      const name = info.sessionName || state.ui.title;
      titleEl.textContent = name || session.title || "pi";
      const bits = [];
      if (state.exited) bits.push("exited");
      else if (state.busy) bits.push("working…");
      else bits.push("idle");
      if (model) bits.push(model);
      if (info.thinkingLevel) bits.push("thinking " + info.thinkingLevel);
      if (info.messageCount) bits.push(info.messageCount + " messages");
      if (info.pendingMessageCount) bits.push(info.pendingMessageCount + " queued");
      if (state.ui.requests.length) bits.push("needs input");
      statusEl.textContent = bits.join(" · ");
      statusEl.classList.toggle("busy", state.busy);
      statusEl.classList.toggle("exited", state.exited);
      statusEl.classList.toggle("needs-input", state.ui.requests.length > 0);
      renderUiStatuses();
      input.disabled = state.exited;
      sendButton.disabled = state.exited;
      stopButton.disabled = state.exited || !state.busy;
      sendLabel.textContent = state.busy ? "Follow up" : "Send";
      sendHint.textContent = state.busy ? "hold to steer" : "";
      sendButton.title = state.busy
        ? "Click or Enter: follow up. Hold or Ctrl+Enter: steer."
        : "Send prompt";
      input.placeholder = state.busy
        ? "pi is working… (Enter to follow up, Ctrl+Enter to steer, Esc to stop)"
        : "Message pi… (Enter to send, Shift+Enter for a newline)";
    }

    const openState = new Map();
    const objectKeys = new WeakMap();
    let nextObjectKey = 0;
    let queueKey = "";

    function keyFor(object, prefix) {
      let key = objectKeys.get(object);
      if (!key) {
        key = prefix + ":" + nextObjectKey++;
        objectKeys.set(object, key);
      }
      return key;
    }

    function collapsible(key, className, summaryText, defaultOpen) {
      const details = el("details", className);
      details.open = openState.has(key) ? openState.get(key) : Boolean(defaultOpen);
      details.addEventListener("toggle", () => openState.set(key, details.open));
      details.appendChild(el("summary", "pi-role", summaryText));
      return details;
    }

    function appendBlock(parent, block, markdown) {
      if (!block || typeof block !== "object") return;
      if (block.type === "text") {
        if (markdown) renderAnswer(parent, block.text || "");
        else renderText(parent, block.text || "");
      } else if (block.type === "thinking") {
        if (block.redacted && !block.thinking) return;
        parent.appendChild(el("div", "pi-text pi-thinking", block.thinking || ""));
      } else if (block.type === "toolCall") {
        const execution = block.execution || (block.id && state.liveTools.get(block.id));
        if (execution) execution.rendered = true;
        const name = block.name || (execution && execution.toolName) || "unknown";
        let summary = "tool · " + name;
        if (execution) {
          summary += execution.isError ? " · error" : execution.done ? " · done" : " · running";
        }
        const details = collapsible(
          "call:" + (block.id || keyFor(block, "call")),
          "pi-tool" + (execution && execution.isError ? " error" : ""),
          summary,
          Boolean(execution)
        );
        let args = "";
        try {
          args = JSON.stringify(
            (execution && execution.args) || block.arguments || {},
            null,
            2
          );
        } catch (e) {
          args = String((execution && execution.args) || block.arguments || "");
        }
        renderCode(details, args, "json", { header: false });
        if (execution && execution.output) {
          renderOutput(details, execution.output, { toolName: name });
        }
        parent.appendChild(details);
      } else if (block.type === "image") {
        parent.appendChild(el("div", "pi-text dim", "[image]"));
      }
    }

    function appendMessage(message, live) {
      if (!message || typeof message !== "object") return;
      const role = message.role || "assistant";
      if (role === "toolResult") {
        const details = collapsible(
          keyFor(message, "result"),
          "pi-msg toolResult" + (live ? " live" : "") + (message.isError ? " error" : " ok"),
          (message.toolName || "tool") + (message.isError ? " result · error" : " result"),
          false
        );
        renderOutput(details, messageText(message), { toolName: message.toolName });
        messagesEl.appendChild(details);
        return;
      }
      const row = el("div", "pi-msg " + role + (live ? " live" : ""));
      if (role === "assistant" || role === "system" || role === "custom") {
        const content = Array.isArray(message.content) ? message.content : [];
        if (role === "assistant" && message.errorMessage) {
          row.appendChild(el("div", "pi-error", message.errorMessage));
        }
        if (!content.length && messageText(message)) {
          if (role === "assistant") renderAnswer(row, messageText(message));
          else renderText(row, messageText(message));
        }
        for (const block of content) appendBlock(row, block, role === "assistant");
      } else {
        row.appendChild(el("div", "pi-role", role));
        renderText(row, messageText(message));
      }
      messagesEl.appendChild(row);
    }

    function appendLiveTool(tool) {
      const status = tool.isError ? "error" : tool.done ? "done" : "running";
      const details = collapsible(
        keyFor(tool, "tool"),
        "pi-tool-live" + (tool.isError ? " error" : ""),
        (tool.toolName || "tool") + " · " + status,
        true
      );
      if (tool.args) renderCode(details, tool.args, "json", { header: false });
      if (tool.output) renderOutput(details, tool.output, { toolName: tool.toolName });
      messagesEl.appendChild(details);
    }

    function appendQueueChip(parent, kind, text) {
      const chip = el("div", "pi-queue-chip " + kind);
      chip.appendChild(el("span", "pi-queue-kind", kind === "steer" ? "steer" : "follow-up"));
      chip.appendChild(el("span", "pi-queue-text", String(text || "")));
      chip.title = String(text || "");
      parent.appendChild(chip);
    }

    function renderQueue() {
      const steering = state.queues.steering || [];
      const followUp = state.queues.followUp || [];
      const total = steering.length + followUp.length;
      const key = JSON.stringify([steering, followUp]);
      if (key === queueKey) return;
      queueKey = key;
      queueEl.replaceChildren();
      queueEl.hidden = total === 0;
      if (!total) return;
      const head = el("div", "pi-queue-head");
      head.appendChild(el("span", "pi-queue-title", total + " queued"));
      const clearButton = el("button", "pi-queue-clear", "Clear");
      clearButton.type = "button";
      clearButton.addEventListener("click", clearQueue);
      head.appendChild(clearButton);
      queueEl.appendChild(head);
      const list = el("div", "pi-queue-list");
      for (const text of steering) appendQueueChip(list, "steer", text);
      for (const text of followUp) appendQueueChip(list, "follow-up", text);
      queueEl.appendChild(list);
    }


    let uiOverlay = null;
    let uiCurrentId = null;
    let uiKeyHandler = null;

    function renderUiStatuses() {
      uiStatusesEl.replaceChildren();
      for (const [key, text] of Object.entries(state.ui.status || {})) {
        const chip = el("span", "pi-ui-status", String(text));
        chip.title = key;
        uiStatusesEl.appendChild(chip);
      }
      uiStatusesEl.hidden = !uiStatusesEl.children.length;
    }

    function renderUiWidgets() {
      widgetsAboveEl.replaceChildren();
      widgetsBelowEl.replaceChildren();
      for (const [key, widget] of Object.entries(state.ui.widgets || {})) {
        if (!widget || !Array.isArray(widget.lines) || !widget.lines.length) continue;
        const box = el("div", "pi-ui-widget");
        box.dataset.key = key;
        for (const line of widget.lines) {
          box.appendChild(el("div", "pi-text", String(line)));
        }
        const target = widget.placement === "belowEditor" ? widgetsBelowEl : widgetsAboveEl;
        target.appendChild(box);
      }
      widgetsAboveEl.hidden = !widgetsAboveEl.children.length;
      widgetsBelowEl.hidden = !widgetsBelowEl.children.length;
    }

    function showToast(message, type) {
      if (!message) return;
      const toast = el("div", "pi-toast " + (type || "info"), String(message));
      toastsEl.appendChild(toast);
      while (toastsEl.children.length > 4) toastsEl.firstChild.remove();
      setTimeout(() => toast.remove(), 6000);
    }

    function removeUiRequest(requestId) {
      state.ui.requests = state.ui.requests.filter((request) => request.id !== requestId);
    }

    function closeUiDialog() {
      if (uiKeyHandler) document.removeEventListener("keydown", uiKeyHandler, true);
      uiKeyHandler = null;
      if (uiOverlay) uiOverlay.remove();
      uiOverlay = null;
      uiCurrentId = null;
    }

    function respondUi(request, response) {
      removeUiRequest(request.id);
      closeUiDialog();
      render();
      if (!options.onUiResponse) {
        setBanner("extension dialogs are unavailable");
        render();
        return;
      }
      Promise.resolve(options.onUiResponse(request.id, response)).catch((error) =>
        commandError(error, true)
      );
    }

    function buildUiDialog(request) {
      const overlay = el("div", "pi-trust-overlay pi-ui-overlay");
      const dialog = el("div", "pi-trust-dialog pi-ui-dialog");
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("aria-label", request.title || "Extension request");

      const heading = el("h2", "", request.title || "Extension request");
      dialog.appendChild(heading);
      if (request.message) {
        dialog.appendChild(el("p", "pi-ui-message", String(request.message)));
      }

      const actions = el("div", "pi-trust-actions pi-ui-actions");
      const cancel = el("button", "", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => respondUi(request, { cancelled: true }));

      if (request.method === "confirm") {
        const no = el("button", "", "No");
        no.type = "button";
        no.addEventListener("click", () => respondUi(request, { confirmed: false }));
        const yes = el("button", "trust", "Yes");
        yes.type = "button";
        yes.addEventListener("click", () => respondUi(request, { confirmed: true }));
        actions.append(cancel, no, yes);
      } else if (request.method === "select") {
        const options = Array.isArray(request.options) ? request.options : [];
        const list = el("div", "pi-ui-options");
        for (const option of options) {
          const button = el("button", "pi-ui-option", String(option));
          button.type = "button";
          button.addEventListener("click", () => respondUi(request, { value: String(option) }));
          list.appendChild(button);
        }
        dialog.appendChild(list);
        actions.append(cancel);
      } else {
        const editor = request.method === "editor";
        const field = el(editor ? "textarea" : "input", "pi-ui-input");
        if (editor) {
          field.rows = 8;
          field.value = typeof request.prefill === "string" ? request.prefill : "";
        } else {
          field.type = "text";
          field.value = typeof request.prefill === "string" ? request.prefill : "";
          field.placeholder = typeof request.placeholder === "string" ? request.placeholder : "";
        }
        const form = el("form", "pi-ui-form");
        const ok = el("button", "trust", "OK");
        ok.type = "submit";
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          respondUi(request, { value: field.value });
        });
        actions.append(cancel, ok);
        form.append(field, actions);
        dialog.appendChild(form);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
        const onKey = (event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            respondUi(request, { cancelled: true });
          }
        };
        document.addEventListener("keydown", onKey, true);
        uiKeyHandler = onKey;
        overlay.addEventListener("pointerdown", (event) => {
          if (event.target === overlay) respondUi(request, { cancelled: true });
        });
        setTimeout(() => field.focus(), 0);
        if (editor && typeof field.setSelectionRange === "function") {
          field.setSelectionRange(field.value.length, field.value.length);
        }
        return overlay;
      }

      dialog.appendChild(actions);
      overlay.appendChild(dialog);
      document.body.appendChild(overlay);
      const onKey = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          respondUi(request, { cancelled: true });
        }
      };
      document.addEventListener("keydown", onKey, true);
      uiKeyHandler = onKey;
      overlay.addEventListener("pointerdown", (event) => {
        if (event.target === overlay) respondUi(request, { cancelled: true });
      });
      const first = actions.querySelector(".trust") || cancel;
      setTimeout(() => first.focus(), 0);
      return overlay;
    }

    function renderUiDialog() {
      const request = state.ui.requests[0];
      if (!request) {
        closeUiDialog();
        return;
      }
      if (uiCurrentId === request.id && uiOverlay) return;
      closeUiDialog();
      uiCurrentId = request.id;
      uiOverlay = buildUiDialog(request);
    }

    function handleUiRequest(request) {
      if (!request || typeof request !== "object" || !request.id) return;
      const method = request.method;
      if (method === "select" || method === "confirm" || method === "input" || method === "editor") {
        removeUiRequest(request.id);
        state.ui.requests.push(request);
        renderUiDialog();
        scheduleRender();
      } else if (method === "notify") {
        showToast(request.message, request.notifyType);
      } else if (method === "setStatus") {
        const key = request.statusKey;
        if (!key) return;
        if (request.statusText) state.ui.status[key] = String(request.statusText);
        else delete state.ui.status[key];
        scheduleRender();
      } else if (method === "setWidget") {
        const key = request.widgetKey;
        if (!key) return;
        if (Array.isArray(request.widgetLines) && request.widgetLines.length) {
          state.ui.widgets[key] = {
            lines: request.widgetLines.map((line) => String(line)),
            placement: request.widgetPlacement || "aboveEditor",
          };
        } else {
          delete state.ui.widgets[key];
        }
        scheduleRender();
      } else if (method === "setTitle") {
        state.ui.title = typeof request.title === "string" ? request.title : "";
        scheduleRender();
      } else if (method === "set_editor_text") {
        if (!input.value.trim() && typeof request.text === "string") {
          input.value = request.text;
          updatePalette();
        }
      }
    }

    function handleUiResolved(requestId, reason) {
      removeUiRequest(requestId);
      if (uiCurrentId === requestId) closeUiDialog();
      if (reason === "timeout") showToast("Extension dialog timed out", "warning");
      scheduleRender();
    }

    function applyUiSnapshot(ui) {
      const data = ui && typeof ui === "object" ? ui : {};
      state.ui.requests = Array.isArray(data.requests) ? data.requests : [];
      state.ui.status = data.status && typeof data.status === "object" ? { ...data.status } : {};
      state.ui.widgets = data.widgets && typeof data.widgets === "object" ? { ...data.widgets } : {};
      state.ui.title = typeof data.title === "string" ? data.title : "";
      renderUiDialog();
    }

    function availableCommands() {
      const items = BUILTIN_COMMANDS.map((command) => ({ ...command }));
      const names = new Set(items.map((command) => command.name));
      for (const command of state.piCommands) {
        if (!command || typeof command.name !== "string" || names.has(command.name)) continue;
        names.add(command.name);
        items.push(command);
      }
      return items;
    }

    function commandQuery() {
      if (state.exited) return null;
      const value = input.value;
      if (!/^\/\S*$/.test(value)) return null;
      return value.slice(1).toLowerCase();
    }

    function closePalette() {
      state.paletteItems = [];
      state.paletteIndex = 0;
      paletteEl.hidden = true;
      paletteEl.replaceChildren();
    }

    function renderPalette() {
      paletteEl.replaceChildren();
      if (!state.paletteItems.length) {
        paletteEl.hidden = true;
        return;
      }
      paletteEl.hidden = false;
      state.paletteItems.forEach((command, index) => {
        const item = el(
          "div",
          "pi-command-item" + (index === state.paletteIndex ? " active" : "")
        );
        item.appendChild(el("span", "pi-command-name", "/" + command.name));
        if (command.description) {
          item.appendChild(el("span", "pi-command-desc", command.description));
        }
        if (command.source) {
          item.appendChild(el("span", "pi-command-source", command.source));
        }
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          acceptCommand(index, false);
        });
        paletteEl.appendChild(item);
      });
      requestAnimationFrame(() => {
        const active = paletteEl.querySelector(".pi-command-item.active");
        if (active && typeof active.scrollIntoView === "function") {
          active.scrollIntoView({ block: "nearest" });
        }
      });
    }

    function updatePalette() {
      const query = commandQuery();
      if (query === null) {
        closePalette();
        return;
      }
      const items = availableCommands().filter((command) =>
        String(command.name).toLowerCase().startsWith(query)
      );
      if (!items.length) {
        closePalette();
        return;
      }
      state.paletteItems = items;
      if (state.paletteIndex >= items.length) state.paletteIndex = 0;
      renderPalette();
    }

    function movePalette(delta) {
      if (!state.paletteItems.length) return;
      state.paletteIndex =
        (state.paletteIndex + delta + state.paletteItems.length) % state.paletteItems.length;
      renderPalette();
    }

    function acceptCommand(index, runExact) {
      const command = state.paletteItems[index];
      if (!command) return;
      const query = commandQuery();
      const exact = query !== null && query === String(command.name).toLowerCase();
      input.value = "/" + command.name + " ";
      if (typeof input.setSelectionRange === "function") {
        input.setSelectionRange(input.value.length, input.value.length);
      }
      input.focus();
      closePalette();
      if (runExact && exact) sendPrompt();
    }

    let scrollAnchor = null;

    function historyRow() {
      const history = state.history;
      const row = el("div", "pi-history");
      if (history.loading) {
        row.appendChild(el("span", "pi-history-status", "Loading earlier messages…"));
      } else if (history.hasMore) {
        const button = el("button", "pi-history-more", "Load earlier messages");
        button.type = "button";
        button.disabled = state.exited;
        button.addEventListener("click", loadEarlier);
        row.appendChild(button);
      }
      if (history.error) {
        row.appendChild(el("span", "pi-history-error", history.error));
      }
      return row;
    }

    function loadEarlier() {
      const history = state.history;
      if (state.exited || history.loading || !history.hasMore || !history.firstId) return;
      if (!options.onHistory) {
        setBanner("earlier messages are unavailable");
        render();
        return;
      }
      history.loading = true;
      history.error = "";
      const generation = history.generation;
      const before = history.firstId;
      const anchor = { height: messagesEl.scrollHeight, top: messagesEl.scrollTop };
      render();
      Promise.resolve(options.onHistory(before, HISTORY_PAGE_SIZE))
        .then((result) => {
          if (generation !== history.generation) return;
          const page = result && Array.isArray(result.entries) ? result.entries : [];
          const known = new Set(
            state.messages.map((item) => item.entryId).filter(Boolean)
          );
          const older = messagesFromEntries(page, known);
          history.loading = false;
          history.hasMore = Boolean(result && result.hasMore);
          if (result && typeof result.firstEntryId === "string") {
            history.firstId = result.firstEntryId;
          } else if (!older.length) {
            history.hasMore = false;
          }
          state.messages = older.concat(state.messages);
          scrollAnchor = anchor;
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
      renderHeader();
      const previousTop = messagesEl.scrollTop;
      const followBottom = stickToBottom;
      for (const tool of state.liveTools.values()) tool.rendered = false;
      messagesEl.replaceChildren();
      if (state.history.hasMore || state.history.loading || state.history.error) {
        messagesEl.appendChild(historyRow());
      }
      for (const item of state.messages) appendMessage(item.message, false);
      if (state.live) appendMessage(state.live, true);
      for (const tool of state.liveTools.values()) {
        if (!tool.rendered) appendLiveTool(tool);
      }
      if (!state.messages.length && !state.live && !state.liveTools.size) {
        messagesEl.appendChild(
          el("div", "pi-empty", "No messages yet. Create a pi session and send a prompt.")
        );
      }
      renderQueue();
      renderUiWidgets();
      if (scrollAnchor) {
        messagesEl.scrollTop =
          messagesEl.scrollHeight - scrollAnchor.height + scrollAnchor.top;
        scrollAnchor = null;
      } else {
        messagesEl.scrollTop = followBottom ? messagesEl.scrollHeight : previousTop;
      }
    }

    let renderQueued = false;
    function scheduleRender() {
      if (renderQueued) return;
      renderQueued = true;
      requestAnimationFrame(() => {
        renderQueued = false;
        render();
      });
    }

    function messagesFromEntries(entries, known) {
      const messages = [];
      for (const entry of entries || []) {
        if (!entry || entry.type !== "message" || !entry.message) continue;
        if (entry.message.role === "system") continue;
        if (known && typeof entry.id === "string") {
          if (known.has(entry.id)) continue;
          known.add(entry.id);
        }
        messages.push({ entryId: entry.id, message: entry.message });
      }
      return messages;
    }

    function reset(message) {
      state.history.generation += 1;
      state.history.loading = false;
      state.history.error = "";
      state.seq = typeof message.seq === "number" ? message.seq : -1;
      state.state = message.state || {};
      state.live = null;
      state.liveTools.clear();
      state.queues = { steering: [], followUp: [] };
      const commands = message.commands && message.commands.commands;
      state.piCommands = Array.isArray(commands)
        ? commands.filter((command) => command && typeof command.name === "string")
        : [];
      state.paletteItems = [];
      state.paletteIndex = 0;
      openState.clear();
      state.busy =
        typeof message.streaming === "boolean"
          ? message.streaming
          : Boolean(state.state.isStreaming);
      state.error = "";
      applyUiSnapshot(message.ui);
      const entryData =
        message.entries && typeof message.entries === "object" ? message.entries : {};
      const entries = Array.isArray(entryData.entries) ? entryData.entries : [];
      state.history.hasMore = Boolean(entryData.hasMore);
      const firstEntry = entries.find(
        (entry) => entry && typeof entry.id === "string"
      );
      state.history.firstId =
        typeof entryData.firstEntryId === "string"
          ? entryData.firstEntryId
          : firstEntry
            ? firstEntry.id
            : null;
      state.messages = messagesFromEntries(entries);
      scrollAnchor = null;
      setBanner("");
      render();
      updatePalette();
    }

    function assistantLive() {
      if (!state.live || state.live.role !== "assistant") {
        state.live = { role: "assistant", content: [] };
      }
      return state.live;
    }

    function updateStream(event) {
      const live = assistantLive();
      if (!Array.isArray(live.content)) live.content = [];
      const index = typeof event.contentIndex === "number" ? event.contentIndex : 0;
      while (live.content.length <= index) live.content.push({ type: "text", text: "" });
      const block = live.content[index];
      switch (event.type) {
        case "text_start":
          live.content[index] = { type: "text", text: "" };
          break;
        case "text_delta":
          if (block.type !== "text") live.content[index] = { type: "text", text: "" };
          live.content[index].text = (live.content[index].text || "") + (event.delta || "");
          break;
        case "text_end":
          live.content[index] = { type: "text", text: event.content || "" };
          break;
        case "thinking_start":
          live.content[index] = { type: "thinking", thinking: "" };
          break;
        case "thinking_delta":
          if (block.type !== "thinking") live.content[index] = { type: "thinking", thinking: "" };
          live.content[index].thinking =
            (live.content[index].thinking || "") + (event.delta || "");
          break;
        case "thinking_end":
          live.content[index] = { type: "thinking", thinking: event.content || "" };
          break;
        case "toolcall_start":
          live.content[index] = {
            type: "toolCall",
            id: event.id,
            name: event.toolName,
            argumentsText: "",
          };
          break;
        case "toolcall_delta":
          if (block.type !== "toolCall") live.content[index] = { type: "toolCall", argumentsText: "" };
          live.content[index].argumentsText =
            (live.content[index].argumentsText || "") + (event.delta || "");
          break;
        case "toolcall_end": {
          const call = event.toolCall || {};
          live.content[index] = {
            type: "toolCall",
            id: call.id,
            name: call.name,
            arguments: call.arguments || {},
          };
          break;
        }
        default:
          break;
      }
    }

    function applyRecord(seq, record) {
      if (typeof seq === "number") {
        if (seq <= state.seq) return;
        state.seq = seq;
      }
      if (!record || typeof record !== "object") return;
      switch (record.type) {
        case "agent_start":
          state.busy = true;
          break;
        case "agent_end":
          break;
        case "agent_settled":
          state.busy = false;
          state.live = null;
          break;
        case "message_start":
          if (record.message && record.message.role === "assistant") {
            state.live = record.message;
          }
          break;
        case "message_update":
          updateStream(record.assistantMessageEvent || {});
          break;
        case "message_end": {
          const message = record.message;
          if (message && message.role === "assistant") {
            state.messages.push({ message });
            state.live = null;
          } else if (message && (message.role === "user" || message.role === "toolResult")) {
            state.messages.push({ message });
          }
          // Once the persisted tool result arrives, render it through the
          // same collapsed ok/error card as a snapshot.  Keeping the live
          // execution around would duplicate its output in the tool-call card.
          if (message && message.role === "toolResult" && message.toolCallId) {
            state.liveTools.delete(message.toolCallId);
          }
          break;
        }
        case "tool_execution_start": {
          const key = record.toolCallId || String(state.liveTools.size);
          state.liveTools.set(key, {
            toolCallId: record.toolCallId,
            toolName: record.toolName,
            args: JSON.stringify(record.args || record.arguments || {}, null, 2),
            output: "",
            done: false,
            isError: false,
            rendered: false,
          });
          break;
        }
        case "tool_execution_update": {
          const key = record.toolCallId;
          const tool = state.liveTools.get(key);
          if (tool) {
            const delta = record.delta || record.output || "";
            if (typeof delta === "string") tool.output += delta;
          }
          break;
        }
        case "tool_execution_end": {
          const key = record.toolCallId;
          const tool = state.liveTools.get(key);
          if (tool) {
            tool.isError = Boolean(record.isError);
            tool.done = true;
            if (typeof record.result === "string") tool.output = record.result;
          }
          break;
        }
        case "compaction_start":
          setBanner("compacting conversation…");
          break;
        case "compaction_end":
          setBanner("");
          break;
        case "auto_retry_start":
          setBanner("retrying after an error…");
          break;
        case "auto_retry_end":
          setBanner("");
          break;
        case "extension_error":
          setBanner("extension error: " + (record.error || "unknown"));
          break;
        case "session_info_changed":
          if (typeof record.name === "string" && record.name) {
            state.state.sessionName = record.name;
          } else {
            delete state.state.sessionName;
          }
          break;
        case "queue_update": {
          if (Array.isArray(record.steering) || Array.isArray(record.followUp)) {
            const steering = Array.isArray(record.steering) ? record.steering : [];
            const followUp = Array.isArray(record.followUp) ? record.followUp : [];
            state.queues = { steering, followUp };
            state.state.pendingMessageCount = steering.length + followUp.length;
          } else if (typeof record.pendingMessageCount === "number") {
            state.state.pendingMessageCount = record.pendingMessageCount;
          }
          break;
        }
        default:
          break;
      }
      scheduleRender();
    }

    function handle(message) {
      if (!message || !message.type) return;
      if (message.type === "pi_resync") {
        reset(message);
      } else if (message.type === "pi_event") {
        applyRecord(message.seq, message.record);
      } else if (message.type === "pi_ui_request") {
        handleUiRequest(message.request);
      } else if (message.type === "pi_ui_resolved") {
        handleUiResolved(message.request_id, message.reason);
      } else if (message.type === "exit") {
        setExited();
      }
    }

    function commandError(error, keepBusy) {
      if (!keepBusy) state.busy = false;
      setBanner(error && error.message ? error.message : "pi command failed");
      render();
    }

    function restoreQueuedText(data) {
      if (!data) return;
      const parts = [];
      for (const key of ["steering", "followUp"]) {
        const items = Array.isArray(data[key]) ? data[key] : [];
        for (const item of items) {
          if (typeof item === "string" && item) parts.push(item);
        }
      }
      if (!parts.length) return;
      const current = input.value.replace(/\s+$/, "");
      input.value = current ? current + "\n" + parts.join("\n") : parts.join("\n");
      if (!state.exited) input.focus();
    }

    function clearQueue() {
      const result = options.onCommand({ type: "clear_queue" });
      Promise.resolve(result)
        .then((response) => {
          restoreQueuedText(response && response.data);
          state.queues = { steering: [], followUp: [] };
          state.state.pendingMessageCount = 0;
          render();
        })
        .catch((error) => commandError(error, true));
    }

    function restoreDraft(text) {
      if (text && !input.value.trim()) input.value = text;
    }

    function showModal(title, body) {
      const overlay = el("div", "pi-modal-overlay");
      const dialog = el("div", "pi-modal");
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("aria-label", title);
      dialog.tabIndex = -1;
      const head = el("div", "pi-modal-head");
      const heading = el("h2", "pi-modal-title", title);
      const close = el("button", "pi-modal-close", "×");
      close.type = "button";
      close.setAttribute("aria-label", "Close");
      head.append(heading, close);
      const content = el("div", "pi-modal-body");
      if (body) content.appendChild(body);
      dialog.append(head, content);
      overlay.appendChild(dialog);
      document.body.appendChild(overlay);

      function closeModal() {
        document.removeEventListener("keydown", onKey, true);
        overlay.remove();
      }
      function onKey(event) {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          closeModal();
        }
      }
      close.addEventListener("click", closeModal);
      overlay.addEventListener("pointerdown", (event) => {
        if (event.target === overlay) closeModal();
      });
      document.addEventListener("keydown", onKey, true);
      dialog.focus();
      return { close: closeModal, content };
    }

    function entryLabel(entry) {
      if (!entry || typeof entry !== "object") return "entry";
      if (entry.type === "message" && entry.message) {
        const role = entry.message.role || "message";
        const text = messageText(entry.message).replace(/\s+/g, " ").trim();
        return role + (text ? ": " + text.slice(0, 120) : "");
      }
      if (entry.type) return entry.type;
      return "entry";
    }

    function afterMutation(result, command, closeModal) {
      return Promise.resolve(result)
        .then((response) => {
          if (response && response.data && response.data.cancelled) return;
          if (closeModal) closeModal();
          return reloadView();
        })
        .catch((error) => {
          if (closeModal) closeModal();
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function forkEntry(entryId, command, closeModal) {
      return afterMutation(
        options.onCommand({ type: "fork", entryId }),
        command,
        closeModal
      );
    }

    function showForkPicker(command) {
      const result = options.onCommand({ type: "get_fork_messages" });
      Promise.resolve(result)
        .then((response) => {
          const messages = (response && response.data && response.data.messages) || [];
          if (!messages.length) {
            setBanner("no user messages available to fork");
            render();
            return;
          }
          const body = el("div", "pi-modal-list");
          const modal = showModal("Fork from a message", body);
          for (const item of messages) {
            const button = el("button", "pi-modal-item");
            button.type = "button";
            const text = String(item.text || "").replace(/\s+/g, " ").trim();
            button.appendChild(el("span", "pi-modal-item-title", text.slice(0, 160)));
            button.addEventListener("click", () =>
              forkEntry(item.entryId, command, modal.close)
            );
            body.appendChild(button);
          }
        })
        .catch((error) => {
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function renderTreeNodes(parent, nodes, leafId, command, modal) {
      for (const node of nodes || []) {
        if (!node || !node.entry) continue;
        const row = el(
          "div",
          "pi-tree-node" + (node.entry.id === leafId ? " active" : "")
        );
        const label = entryLabel(node.entry);
        row.appendChild(el("span", "pi-tree-label", label));
        if (node.entry.id === leafId) {
          row.appendChild(el("span", "pi-tree-leaf", "active"));
        }
        if (node.entry.type === "message" && node.entry.message.role === "user") {
          const fork = el("button", "pi-tree-fork", "fork");
          fork.type = "button";
          fork.title = "Fork from this message";
          fork.addEventListener("click", () =>
            forkEntry(node.entry.id, command, modal.close)
          );
          row.appendChild(fork);
        }
        parent.appendChild(row);
        if (node.children && node.children.length) {
          const children = el("div", "pi-tree-children");
          renderTreeNodes(children, node.children, leafId, command, modal);
          parent.appendChild(children);
        }
      }
    }

    function showTree(command) {
      const result = options.onCommand({ type: "get_tree" });
      Promise.resolve(result)
        .then((response) => {
          const data = (response && response.data) || {};
          const body = el("div", "pi-tree");
          const modal = showModal("Session tree", body);
          if (!Array.isArray(data.tree) || !data.tree.length) {
            body.appendChild(el("div", "pi-empty", "The session tree is empty."));
            return;
          }
          renderTreeNodes(body, data.tree, data.leafId, command, modal);
        })
        .catch((error) => {
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function currentModel() {
      const model = state.state && state.state.model;
      return model && typeof model === "object" ? model : null;
    }

    function modelName(model) {
      return model.name && model.name !== model.id ? model.name : model.id;
    }

    function modelMatches(model, query) {
      if (!query) return true;
      const haystack = [model.provider, model.id, model.name]
        .filter((part) => typeof part === "string")
        .join(" ")
        .toLowerCase();
      return haystack.includes(query.toLowerCase());
    }

    function fetchModels() {
      return Promise.resolve(options.onCommand({ type: "get_available_models" })).then(
        (response) => {
          const models = response && response.data && response.data.models;
          if (!Array.isArray(models)) return [];
          return models.filter(
            (model) =>
              model &&
              typeof model === "object" &&
              typeof model.id === "string" &&
              typeof model.provider === "string"
          );
        }
      );
    }

    function selectModel(model, command) {
      setBanner("switching to " + model.provider + "/" + model.id + "…");
      render();
      return Promise.resolve(
        options.onCommand({
          type: "set_model",
          provider: model.provider,
          modelId: model.id,
        })
      )
        .then((response) => {
          const applied = response && response.data;
          state.state = {
            ...state.state,
            model: applied && applied.id ? applied : model,
          };
          setBanner("");
          render();
        })
        .catch((error) => {
          restoreDraft(command && command.text);
          commandError(error, false);
        });
    }

    function showModelPicker(command, query, provider, available) {
      Promise.resolve(available || fetchModels())
        .then((all) => {
          const models = provider
            ? all.filter((model) => model.provider === provider)
            : all;
          if (!models.length) {
            setBanner(provider ? "no models for provider " + provider : "no models available");
            render();
            return;
          }
          const current = currentModel();
          const body = el("div", "pi-model-picker");
          const search = el("input", "pi-model-search");
          search.type = "search";
          search.placeholder = "Filter by provider, model, or name…";
          search.value = query || "";
          const list = el("div", "pi-modal-list");
          body.append(search, list);
          const modal = showModal(
            provider ? "Models · " + provider : "Select a model",
            body
          );

          function visible() {
            return models.filter((model) => modelMatches(model, search.value.trim()));
          }

          function renderList() {
            list.replaceChildren();
            const items = visible();
            if (!items.length) {
              list.appendChild(el("div", "pi-empty", "No matching models."));
              return;
            }
            const groups = new Map();
            for (const model of items) {
              if (!groups.has(model.provider)) groups.set(model.provider, []);
              groups.get(model.provider).push(model);
            }
            for (const [name, group] of groups) {
              list.appendChild(el("div", "pi-model-group", name));
              for (const model of group) {
                const chosen =
                  current &&
                  current.id === model.id &&
                  current.provider === model.provider;
                const button = el("button", "pi-modal-item");
                button.type = "button";
                const head = el("span", "pi-model-head");
                head.appendChild(el("span", "pi-modal-item-title", modelName(model)));
                if (chosen) head.appendChild(el("span", "pi-model-current", "current"));
                button.appendChild(head);
                button.appendChild(
                  el("span", "pi-model-detail", model.provider + "/" + model.id)
                );
                button.addEventListener("click", () => {
                  modal.close();
                  selectModel(model, command);
                });
                list.appendChild(button);
              }
            }
          }

          search.addEventListener("input", renderList);
          search.addEventListener("search", renderList);
          search.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            const first = visible()[0];
            if (first) {
              modal.close();
              selectModel(first, command);
            }
          });
          renderList();
          search.focus();
        })
        .catch((error) => {
          restoreDraft(command && command.text);
          commandError(error, false);
        });
    }

    function showProviderPicker(command, query, available) {
      Promise.resolve(available || fetchModels())
        .then((models) => {
          const byProvider = new Map();
          for (const model of models) {
            if (!byProvider.has(model.provider)) byProvider.set(model.provider, []);
            byProvider.get(model.provider).push(model);
          }
          if (!byProvider.size) {
            setBanner("no providers available");
            render();
            return;
          }
          const providers = [...byProvider.keys()].sort();
          if (query) {
            const exact = providers.find(
              (provider) => provider.toLowerCase() === query.toLowerCase()
            );
            if (exact) {
              showModelPicker(command, "", exact, models);
              return;
            }
          }
          const matches = query
            ? providers.filter((provider) =>
                provider.toLowerCase().includes(query.toLowerCase())
              )
            : providers;
          if (!matches.length) {
            setBanner("no provider matches " + query);
            render();
            return;
          }
          const current = currentModel();
          const body = el("div", "pi-modal-list");
          const modal = showModal(
            query ? "Providers matching " + query : "Select a provider",
            body
          );
          for (const provider of matches) {
            const count = byProvider.get(provider).length;
            const button = el("button", "pi-modal-item");
            button.type = "button";
            const head = el("span", "pi-model-head");
            head.appendChild(el("span", "pi-modal-item-title", provider));
            if (current && current.provider === provider) {
              head.appendChild(el("span", "pi-model-current", "current"));
            }
            button.appendChild(head);
            button.appendChild(
              el("span", "pi-model-detail", count + (count === 1 ? " model" : " models"))
            );
            button.addEventListener("click", () => {
              modal.close();
              showModelPicker(command, "", provider, models);
            });
            body.appendChild(button);
          }
        })
        .catch((error) => {
          restoreDraft(command && command.text);
          commandError(error, false);
        });
    }

    function runModelCommand(command) {
      if (!command.args) {
        showModelPicker(command, "", null);
        return;
      }
      const query = command.args;
      fetchModels()
        .then((models) => {
          const match = models.find(
            (model) =>
              (model.provider + "/" + model.id).toLowerCase() === query.toLowerCase()
          );
          if (match) selectModel(match, command);
          else showModelPicker(command, query, null, models);
        })
        .catch((error) => {
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function runProviderCommand(command) {
      showProviderPicker(command, command.args);
    }

    function currentThinkingLevel() {
      const level = state.state && state.state.thinkingLevel;
      return typeof level === "string" ? level : "";
    }

    function fetchThinkingLevels() {
      return Promise.resolve(
        options.onCommand({ type: "get_available_thinking_levels" })
      ).then((response) => {
        const levels = response && response.data && response.data.levels;
        if (!Array.isArray(levels)) return [];
        return levels.filter((level) => typeof level === "string" && level);
      });
    }

    function selectThinkingLevel(level, command) {
      setBanner("setting thinking to " + level + "…");
      render();
      return Promise.resolve(options.onCommand({ type: "set_thinking_level", level }))
        .then(() => {
          // pi reports the level back through get_state on the next snapshot;
          // track it locally so the header and picker update immediately.
          state.state = { ...state.state, thinkingLevel: level };
          setBanner("");
          showToast("thinking level: " + level, "info");
          render();
        })
        .catch((error) => {
          restoreDraft(command && command.text);
          commandError(error, false);
        });
    }

    function showThinkingPicker(command, query, available) {
      Promise.resolve(available || fetchThinkingLevels())
        .then((levels) => {
          if (!levels.length) {
            setBanner("the current model has no thinking levels");
            render();
            return;
          }
          const matches = query
            ? levels.filter((level) =>
                level.toLowerCase().startsWith(query.toLowerCase())
              )
            : levels;
          if (!matches.length) {
            setBanner("no thinking level matches " + query);
            render();
            return;
          }
          const current = currentThinkingLevel();
          const body = el("div", "pi-modal-list");
          const modal = showModal("Thinking level", body);
          for (const level of matches) {
            const button = el("button", "pi-modal-item");
            button.type = "button";
            const head = el("span", "pi-model-head");
            head.appendChild(el("span", "pi-modal-item-title", level));
            if (level === current) {
              head.appendChild(el("span", "pi-model-current", "current"));
            }
            button.appendChild(head);
            button.addEventListener("click", () => {
              modal.close();
              if (level !== current) selectThinkingLevel(level, command);
            });
            body.appendChild(button);
          }
        })
        .catch((error) => {
          restoreDraft(command && command.text);
          commandError(error, false);
        });
    }

    function runThinkingCommand(command) {
      const query = (command.args || "").trim();
      if (!query) {
        showThinkingPicker(command, "");
        return;
      }
      fetchThinkingLevels()
        .then((levels) => {
          const exact = levels.find(
            (level) => level.toLowerCase() === query.toLowerCase()
          );
          if (exact) {
            selectThinkingLevel(exact, command);
            return;
          }
          showThinkingPicker(command, query, levels);
        })
        .catch((error) => {
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function downloadExport(format, command) {
      if (!options.onExport) {
        setBanner("export is unavailable");
        render();
        return;
      }
      setBanner("exporting…");
      render();
      Promise.resolve(options.onExport(format))
        .then((data) => {
          if (!data || !data.url) throw new Error("export produced no file");
          const link = document.createElement("a");
          link.href = data.url;
          link.download = "";
          document.body.appendChild(link);
          link.click();
          link.remove();
          setBanner("");
          render();
        })
        .catch((error) => {
          restoreDraft(command.text);
          commandError(error, false);
        });
    }

    function reloadView() {
      if (!options.onRefresh) {
        setBanner("reload is unavailable");
        render();
        return Promise.resolve();
      }
      setBanner("reloading…");
      render();
      return Promise.resolve(options.onRefresh()).catch((error) =>
        commandError(error, false)
      );
    }

    function builtinSlash(text) {
      if (!text.startsWith("/")) return null;
      const match = /^\/([^\s]+)(?:\s+([\s\S]*))?$/.exec(text);
      if (!match) return null;
      const name = match[1].toLowerCase();
      if (!BUILTIN_NAMES.has(name)) return null;
      return { name, args: (match[2] || "").trim(), text };
    }

    function runSlashCommand(text) {
      const command = builtinSlash(text);
      if (!command) return false;
      // Changing the thinking level is safe mid-run: pi applies it to the next
      // model call, so there is no reason to make the user stop first.
      const passive =
        command.name === "export" ||
        command.name === "tree" ||
        command.name === "thinking";
      if (state.busy && !passive) {
        setBanner("stop pi before running /" + command.name);
        render();
        return true;
      }
      input.value = "";
      if (command.name === "reload") {
        reloadView();
        return true;
      }
      if (command.name === "model") {
        runModelCommand(command);
        return true;
      }
      if (command.name === "provider") {
        runProviderCommand(command);
        return true;
      }
      if (command.name === "thinking") {
        runThinkingCommand(command);
        return true;
      }
      if (command.name === "export") {
        const format = command.args && command.args.toLowerCase() === "jsonl" ? "jsonl" : "html";
        downloadExport(format, command);
        return true;
      }
      if (command.name === "tree") {
        showTree(command);
        return true;
      }
      if (command.name === "fork") {
        showForkPicker(command);
        return true;
      }
      if (command.name === "clone") {
        afterMutation(options.onCommand({ type: "clone" }), command, null);
        return true;
      }
      if (command.name === "new") {
        afterMutation(options.onCommand({ type: "new_session" }), command, null);
        return true;
      }
      const rpc = { type: "compact" };
      if (command.args) rpc.customInstructions = command.args;
      const result = options.onCommand(rpc);
      Promise.resolve(result).catch((error) => {
        restoreDraft(command.text);
        commandError(error, false);
      });
      return true;
    }

    function sendPrompt(behavior) {
      const text = input.value.trim();
      if (!text || state.exited) return;
      closePalette();
      if (runSlashCommand(text)) return;
      const wasBusy = state.busy;
      const command = { type: "prompt", message: text };
      if (wasBusy) command.streamingBehavior = behavior || "followUp";
      input.value = "";
      setBanner("");
      if (!wasBusy) state.busy = true;
      const result = options.onCommand(command);
      if (result && typeof result.catch === "function") {
        result.catch((error) => {
          restoreDraft(text);
          commandError(error, wasBusy);
        });
      }
      render();
    }

    function abort() {
      if (state.exited || !state.busy) return;
      const result = options.onCommand({ type: "clear_queue" });
      Promise.resolve(result)
        .then((response) => {
          restoreQueuedText(response && response.data);
          state.queues = { steering: [], followUp: [] };
          state.state.pendingMessageCount = 0;
          render();
          return options.onCommand({ type: "abort" });
        })
        .catch((error) => commandError(error, true));
    }

    function setExited() {
      state.exited = true;
      state.busy = false;
      state.live = null;
      state.liveTools.clear();
      state.queues = { steering: [], followUp: [] };
      state.state.pendingMessageCount = 0;
      state.ui = { requests: [], status: {}, widgets: {}, title: "" };
      closePalette();
      closeUiDialog();
      render();
    }

    const LONG_PRESS_MS = 500;
    let longPressTimer = null;
    let suppressClick = false;

    function cancelLongPress() {
      if (longPressTimer !== null) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }
    }

    sendButton.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      suppressClick = false;
      cancelLongPress();
      longPressTimer = setTimeout(() => {
        longPressTimer = null;
        suppressClick = true;
        sendPrompt("steer");
      }, LONG_PRESS_MS);
    });
    sendButton.addEventListener("pointerup", cancelLongPress);
    sendButton.addEventListener("pointercancel", cancelLongPress);
    sendButton.addEventListener("pointerleave", cancelLongPress);
    sendButton.addEventListener("contextmenu", (event) => event.preventDefault());
    sendButton.addEventListener("click", (event) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendPrompt();
    });
    input.addEventListener("input", updatePalette);
    input.addEventListener("click", updatePalette);
    input.addEventListener("blur", () => setTimeout(closePalette, 0));
    input.addEventListener("keydown", (event) => {
      if (!paletteEl.hidden) {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          movePalette(1);
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          movePalette(-1);
          return;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          acceptCommand(state.paletteIndex, false);
          return;
        }
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          acceptCommand(state.paletteIndex, !event.ctrlKey && !event.metaKey);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          closePalette();
          return;
        }
      }
      if (event.key === "Enter") {
        if (event.ctrlKey || event.metaKey) {
          event.preventDefault();
          sendPrompt("steer");
        } else if (!event.shiftKey) {
          event.preventDefault();
          sendPrompt();
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        abort();
      }
    });
    stopButton.addEventListener("click", abort);

    render();

    return {
      element: root,
      handle,
      setExited,
      activate: () => {
        if (!state.exited) input.focus();
      },
      destroy: () => {
        closeUiDialog();
        root.remove();
      },
    };
  }

  window.WebtermPi = { create };
})();
