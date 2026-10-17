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

  function create(session, options) {
    const root = el("div", "pi-view");
    const header = el("div", "pi-header");
    const titleEl = el("span", "pi-title", session.title || "pi");
    const statusEl = el("span", "pi-status", "starting…");
    header.append(titleEl, statusEl);

    const banner = el("div", "pi-banner");
    banner.hidden = true;
    const messagesEl = el("div", "pi-messages");
    const queueEl = el("div", "pi-queue");
    queueEl.hidden = true;

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

    root.append(header, banner, messagesEl, queueEl, form);

    const state = {
      seq: -1,
      messages: [],
      live: null,
      liveTools: new Map(),
      queues: { steering: [], followUp: [] },
      state: {},
      busy: false,
      exited: false,
      banner: "",
      error: "",
    };

    function setBanner(text) {
      state.banner = text || "";
      banner.textContent = state.banner;
      banner.hidden = !state.banner;
    }

    function renderHeader() {
      const info = state.state || {};
      const model = info.model && (info.model.id || info.model.name);
      const name = info.sessionName;
      if (name) titleEl.textContent = name;
      const bits = [];
      if (state.exited) bits.push("exited");
      else if (state.busy) bits.push("working…");
      else bits.push("idle");
      if (model) bits.push(model);
      if (info.thinkingLevel) bits.push("thinking " + info.thinkingLevel);
      if (info.messageCount) bits.push(info.messageCount + " messages");
      if (info.pendingMessageCount) bits.push(info.pendingMessageCount + " queued");
      statusEl.textContent = bits.join(" · ");
      statusEl.classList.toggle("busy", state.busy);
      statusEl.classList.toggle("exited", state.exited);
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

    function appendBlock(parent, block) {
      if (!block || typeof block !== "object") return;
      if (block.type === "text") {
        parent.appendChild(el("div", "pi-text", block.text || ""));
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
        details.appendChild(el("pre", "pi-pre", args));
        if (execution && execution.output) {
          details.appendChild(el("pre", "pi-pre", execution.output));
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
        details.appendChild(el("pre", "pi-pre", messageText(message)));
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
          row.appendChild(el("div", "pi-text", messageText(message)));
        }
        for (const block of content) appendBlock(row, block);
      } else {
        row.appendChild(el("div", "pi-role", role));
        row.appendChild(el("div", "pi-text", messageText(message)));
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
      if (tool.args) details.appendChild(el("pre", "pi-pre", tool.args));
      if (tool.output) details.appendChild(el("pre", "pi-pre", tool.output));
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

    function render() {
      renderHeader();
      for (const tool of state.liveTools.values()) tool.rendered = false;
      messagesEl.replaceChildren();
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
      messagesEl.scrollTop = messagesEl.scrollHeight;
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

    function reset(message) {
      state.seq = typeof message.seq === "number" ? message.seq : -1;
      state.state = message.state || {};
      state.live = null;
      state.liveTools.clear();
      state.queues = { steering: [], followUp: [] };
      openState.clear();
      state.busy = Boolean(state.state.isStreaming);
      state.error = "";
      const entries = (message.entries && message.entries.entries) || [];
      state.messages = [];
      for (const entry of entries) {
        if (!entry || entry.type !== "message" || !entry.message) continue;
        if (entry.message.role === "system") continue;
        state.messages.push({ entryId: entry.id, message: entry.message });
      }
      setBanner("");
      render();
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
          } else if (message && message.role === "user") {
            state.messages.push({ message });
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

    function sendPrompt(behavior) {
      const text = input.value.trim();
      if (!text || state.exited) return;
      const wasBusy = state.busy;
      const command = { type: "prompt", message: text };
      if (wasBusy) command.streamingBehavior = behavior || "followUp";
      input.value = "";
      setBanner("");
      if (!wasBusy) state.busy = true;
      const result = options.onCommand(command);
      if (result && typeof result.catch === "function") {
        result.catch((error) => {
          if (!input.value.trim()) input.value = text;
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
    input.addEventListener("keydown", (event) => {
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
      destroy: () => root.remove(),
    };
  }

  window.WebtermPi = { create };
})();
