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

  /** Placeholder carried by the first option of the saved-prompt picker. */
  const PROMPT_PLACEHOLDER = "saved prompts…";

  /** How long "Delete <name>?" stays armed before it resets itself. */
  const PROMPT_DELETE_ARM_MS = 4000;

  /** Thinking levels used when an adapter does not advertise its own list. */
  const FALLBACK_THINKING_LEVELS = [
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
  ];

  /** Slash commands the canonical command set can run.  Each is gated on the
   * capability that makes its control exist, so the palette never offers a
   * command the adapter would refuse or the view has no target for.  `/limits`,
   * tree/fork/clone, and export stay in the legacy pi view for later phases. */
  const PALETTE_COMMANDS = [
    { name: "compact", description: "Compact the conversation" },
    { name: "model", description: "Switch model" },
    { name: "thinking", description: "Set the thinking level" },
    { name: "system", description: "Edit the system prompt" },
  ];

  /** Characters of a queued message shown in a chip; the full text is the
   * chip's title (and the CSS ellipsis keeps the row one line). */
  const QUEUE_CHIP_TEXT_LIMIT = 120;

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
    const onCommand =
      typeof options.onCommand === "function" ? options.onCommand : null;
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
    const findButton = el("button", "pi-find-toggle", "find");
    findButton.type = "button";
    findButton.title = "Search this transcript (Ctrl+F)";
    header.append(titleEl, metaEl, findButton);

    const controls = el("div", "agent-controls");
    const modelSelect = el("select", "agent-control agent-model");
    modelSelect.title = "Model for the next reply";
    modelSelect.setAttribute("aria-label", "Model");
    const thinkingSelect = el("select", "agent-control agent-thinking");
    thinkingSelect.title = "Thinking effort for the next reply";
    thinkingSelect.setAttribute("aria-label", "Thinking effort");
    const systemButton = el("button", "agent-system-toggle", "system prompt");
    systemButton.type = "button";
    systemButton.title = "Edit the system prompt used for this chat";
    controls.append(modelSelect, thinkingSelect, systemButton);

    const systemPanel = el("div", "agent-system-panel");
    systemPanel.hidden = true;
    const systemLabel = el("label", "agent-system-label", "System prompt");
    const systemInput = el("textarea", "agent-system-input");
    systemInput.rows = 4;
    systemInput.placeholder =
      "Instructions sent as the system message for every request. Empty clears it.";
    systemLabel.append(systemInput);
    const systemActions = el("div", "agent-system-actions");
    const systemSave = el("button", "agent-system-save", "Save");
    systemSave.type = "button";
    const systemClear = el("button", "agent-system-clear", "Clear");
    systemClear.type = "button";
    const systemClose = el("button", "agent-system-close", "Cancel");
    systemClose.type = "button";
    systemActions.append(systemSave, systemClear, systemClose);

    // Saved-prompt library: picker plus inline save/delete actions.  The whole
    // row is capability-gated (see syncControls) and lists lazily on open.
    const promptRow = el("div", "agent-system-library");
    promptRow.hidden = true;
    const promptLine = el("div", "agent-system-library-row");
    const promptSelect = el("select", "agent-control agent-prompt-pick");
    promptSelect.title = "Saved system prompts";
    promptSelect.setAttribute("aria-label", "Saved prompts");
    const promptPlaceholder = el("option", "", PROMPT_PLACEHOLDER);
    promptPlaceholder.value = "";
    promptSelect.appendChild(promptPlaceholder);
    const promptSaveButton = el("button", "agent-system-library-save", "Save to library");
    promptSaveButton.type = "button";
    promptSaveButton.title = "Save the text above as a named prompt";
    const promptDeleteButton = el("button", "agent-system-library-delete", "Delete");
    promptDeleteButton.type = "button";
    promptDeleteButton.title = "Delete the selected saved prompt";
    promptLine.append(promptSelect, promptSaveButton, promptDeleteButton);
    const promptForm = el("div", "agent-system-library-form");
    promptForm.hidden = true;
    const promptNameInput = el("input", "agent-control agent-system-library-name");
    promptNameInput.type = "text";
    promptNameInput.placeholder = "prompt name";
    promptNameInput.setAttribute("aria-label", "Prompt name");
    const promptCommit = el("button", "agent-system-library-commit", "Save");
    promptCommit.type = "button";
    const promptCancel = el("button", "agent-system-library-cancel", "Cancel");
    promptCancel.type = "button";
    promptForm.append(promptNameInput, promptCommit, promptCancel);
    promptRow.append(promptLine, promptForm);
    systemPanel.append(promptRow, systemLabel, systemActions);

    const banner = el("div", "pi-banner agent-banner");
    banner.hidden = true;
    const findHost = el("div", "find-host");
    const messagesEl = el("div", "pi-messages agent-messages");
    const dialogHost = el("div", "agent-dialog-host");
    dialogHost.hidden = true;
    // Queued steering/follow-up text sits directly above the composer, with the
    // slash-command palette between them (mirroring the pi view).
    const queueEl = el("div", "pi-queue agent-queue");
    queueEl.hidden = true;
    const paletteEl = el("div", "pi-command-palette agent-palette");
    paletteEl.hidden = true;

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
    // Image input is capability-gated in updateStatus(); the controller owns
    // paste, drop, and the thumbnail strip.
    const attach = window.WebtermAttach
      ? window.WebtermAttach.create({
          form,
          input,
          onNotice: (text) => showNotice(text, "warning"),
        })
      : null;
    if (attach) form.append(attach.element);
    form.append(input, actions);

    // Literal `@path` completion; the path is plain text, not an expansion.
    const pathComplete =
      typeof options.onListEntries === "function" && window.WebtermPathComplete
        ? window.WebtermPathComplete.create({
            input: input,
            basePath: session.cwd || "",
            listEntries: options.onListEntries,
          })
        : null;

    root.append(
      header,
      controls,
      systemPanel,
      banner,
      findHost,
      messagesEl,
      dialogHost,
      queueEl,
      paletteEl,
      ...(pathComplete ? [pathComplete.element] : []),
      form,
      footer
    );

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
      // Pending input reported by ``queue.updated``: canonical steering and
      // follow-up text, both rendered as chips above the composer.
      queues: { steering: [], followUp: [] },
      // Open slash-command palette: the filtered entries and the active row.
      palette: { items: [], index: 0 },
      systemOpen: false,
      prompts: {
        list: [],
        selected: "",
        loading: false,
        saving: false,
        nameOpen: false,
        armed: false,
        armTimer: null,
        generation: 0,
      },
      usage: { tokens: 0, cost: 0 },
      // One past turn at a time is editable: which entry, its draft text, and
      // whether an edit_message command is in flight.
      edit: { id: null, draft: "", saving: false },
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

    function modelsCapability() {
      const caps = state.capabilities || {};
      const models = caps.models;
      return models && typeof models === "object" ? models : {};
    }

    function thinkingLevels() {
      const models = modelsCapability();
      const levels = Array.isArray(models.levels)
        ? models.levels.filter((level) => typeof level === "string" && level)
        : [];
      return levels.length ? levels : FALLBACK_THINKING_LEVELS;
    }

    function showModelsError(error) {
      showNotice(
        error && error.message ? error.message : "could not load models",
        "error"
      );
    }

    let modelsLoaded = false;
    let thinkingSignature = "";

    function loadModels() {
      if (modelsLoaded || !onCommand) return;
      const models = modelsCapability();
      if (!models.list) return;
      modelsLoaded = true;
      Promise.resolve(onCommand({ type: "list_models" })).then(
        (data) => {
          const list = data && Array.isArray(data.models) ? data.models : [];
          modelSelect.innerHTML = "";
          for (const model of list) {
            if (!model || typeof model.id !== "string" || !model.id) continue;
            const label = model.name ? model.name + " (" + model.id + ")" : model.id;
            const option = el("option", "", label);
            option.value = model.id;
            modelSelect.appendChild(option);
          }
          modelSelect.value = state.meta.model || session.model || "";
          syncControls();
        },
        (error) => {
          modelsLoaded = false;
          showModelsError(error);
          scheduleRender();
        }
      );
    }

    function syncControls() {
      const models = modelsCapability();
      const showModels = Boolean(models.list && onCommand);
      modelSelect.hidden = !showModels;
      if (showModels && !modelSelect.children.length) {
        const option = el("option", "", "loading models…");
        option.value = "";
        modelSelect.appendChild(option);
      }

      const showThinking = Boolean(models.thinking && onCommand);
      thinkingSelect.hidden = !showThinking;
      if (showThinking) {
        const levels = thinkingLevels();
        const signature = levels.join("\n");
        if (signature !== thinkingSignature) {
          thinkingSignature = signature;
          thinkingSelect.innerHTML = "";
          for (const level of levels) {
            const label = level === "default" ? "thinking: default" : "thinking: " + level;
            const option = el("option", "", label);
            option.value = level;
            thinkingSelect.appendChild(option);
          }
        }
        const level = state.meta.thinking_level;
        if (typeof level === "string" && level) thinkingSelect.value = level;
      }

      const showSystem = Boolean(state.capabilities.system_prompt && onCommand);
      systemButton.hidden = !showSystem;
      if (!showSystem) {
        state.systemOpen = false;
        closePromptUi();
      }
      systemButton.classList.toggle("set", Boolean(state.meta.system_prompt));
      systemButton.textContent = state.meta.system_prompt
        ? "system prompt · set"
        : "system prompt";
      systemPanel.hidden = !state.systemOpen;

      const showLibrary = promptsEnabled();
      promptRow.hidden = !showLibrary;
      if (!showLibrary) closePromptUi();
      const libraryBusy = state.prompts.loading || state.prompts.saving;
      const selectedPrompt = savedPromptName();
      promptSelect.disabled = state.exited || libraryBusy;
      promptSaveButton.disabled = state.exited || libraryBusy;
      promptDeleteButton.disabled = state.exited || libraryBusy || !selectedPrompt;
      const armed = Boolean(state.prompts.armed && selectedPrompt);
      promptDeleteButton.textContent = armed ? "Delete " + selectedPrompt + "?" : "Delete";
      promptDeleteButton.classList.toggle("armed", armed);
      promptNameInput.disabled = state.exited || libraryBusy;
      promptCommit.disabled = state.exited || libraryBusy;
      promptCancel.disabled = state.exited || libraryBusy;
      promptForm.hidden = !(showLibrary && state.prompts.nameOpen);

      controls.hidden =
        modelSelect.hidden && thinkingSelect.hidden && systemButton.hidden;
      modelSelect.disabled = state.exited || !models.set;
      thinkingSelect.disabled = state.exited;
      systemButton.disabled = state.exited;
      systemSave.disabled = state.exited;
      systemClear.disabled = state.exited;
    }

    /** Whether a queued-input command can be sent at all; the canonical set
     * advertises it as ``queue``/``steering``/``follow_up``. */
    function queueEnabled() {
      return Boolean(onCommand);
    }

    /** Append one queued-message chip: kind label plus truncated text, with the
     * full text kept in ``title``.  Everything is built with textContent. */
    function appendQueueChip(parent, kind, text) {
      const full = typeof text === "string" ? text : String(text || "");
      const shown =
        full.length > QUEUE_CHIP_TEXT_LIMIT
          ? full.slice(0, QUEUE_CHIP_TEXT_LIMIT - 1) + "…"
          : full;
      const chip = el("div", "pi-queue-chip agent-queue-chip " + kind);
      chip.appendChild(
        el("span", "pi-queue-kind agent-queue-kind", kind === "steer" ? "steer" : "follow-up")
      );
      chip.appendChild(el("span", "pi-queue-text agent-queue-text", shown));
      chip.title = full;
      parent.appendChild(chip);
    }

    let queueSignature = "";

    /** Rebuild the chip row only when the queue itself changed, so unrelated
     * renders keep the Clear button (and any focus on it) untouched. */
    function renderQueue() {
      const steering = state.queues.steering || [];
      const followUp = state.queues.followUp || [];
      const total = steering.length + followUp.length;
      const signature = JSON.stringify([steering, followUp]);
      if (signature === queueSignature) return;
      queueSignature = signature;
      queueEl.innerHTML = "";
      queueEl.hidden = total === 0;
      if (!total) return;
      const head = el("div", "pi-queue-head agent-queue-head");
      head.appendChild(el("span", "pi-queue-title agent-queue-title", total + " queued"));
      if (queueEnabled()) {
        const clearButton = el("button", "pi-queue-clear agent-queue-clear", "Clear");
        clearButton.type = "button";
        clearButton.title = "Clear queued messages and put them back in the composer";
        clearButton.addEventListener("click", clearQueue);
        head.appendChild(clearButton);
      }
      queueEl.appendChild(head);
      const list = el("div", "pi-queue-list agent-queue-list");
      for (const text of steering) appendQueueChip(list, "steer", text);
      for (const text of followUp) appendQueueChip(list, "follow-up", text);
      queueEl.appendChild(list);
    }

    /** Put the adapter's returned queued text back into the composer.  pi
     * answers ``{steering, followUp}``; a native adapter may use the canonical
     * ``follow_up`` spelling, so both are read. */
    function restoreQueuedText(data) {
      if (!data || typeof data !== "object") return;
      const steering = Array.isArray(data.steering) ? data.steering : [];
      const followUp = Array.isArray(data.followUp)
        ? data.followUp
        : Array.isArray(data.follow_up)
          ? data.follow_up
          : [];
      const parts = [];
      for (const item of steering.concat(followUp)) {
        if (typeof item === "string" && item) parts.push(item);
      }
      if (!parts.length) return;
      const current = input.value.replace(/\s+$/, "");
      input.value = current ? current + "\n" + parts.join("\n") : parts.join("\n");
      if (!state.exited && typeof input.focus === "function") input.focus();
    }

    function clearQueue() {
      if (!queueEnabled()) return;
      Promise.resolve(onCommand({ type: "clear_queue" })).then(
        (data) => {
          restoreQueuedText(data);
          state.queues = { steering: [], followUp: [] };
          scheduleRender();
        },
        (error) => {
          showNotice(
            error && error.message ? error.message : "could not clear the queue",
            "error"
          );
        }
      );
    }

    /** Esc and Stop clear first, so queued text is not lost when the run that
     * would have carried it is aborted.  A refused clear keeps the run alive,
     * matching the pi view. */
    function abort() {
      if (state.exited || !state.busy) return;
      const cleared = queueEnabled()
        ? Promise.resolve(onCommand({ type: "clear_queue" })).then((data) => {
            restoreQueuedText(data);
            state.queues = { steering: [], followUp: [] };
            scheduleRender();
          })
        : Promise.resolve();
      cleared.then(
        () => {
          const promise = onAbort();
          if (promise && promise.catch) {
            promise.catch((error) =>
              showNotice(error && error.message ? error.message : "abort failed", "error")
            );
          }
        },
        (error) => {
          showNotice(
            error && error.message ? error.message : "could not clear the queue",
            "error"
          );
        }
      );
    }

    /** Palette entries the current capabilities allow.  Each maps onto a
     * control that already exists, or onto one canonical command. */
    function availableCommands() {
      const caps = state.capabilities || {};
      const models = modelsCapability();
      const items = [];
      for (const command of PALETTE_COMMANDS) {
        if (command.name === "compact") {
          if (!(caps.compaction && onCommand)) continue;
        } else if (command.name === "model") {
          if (!(models.set && onCommand)) continue;
        } else if (command.name === "thinking") {
          if (!(models.thinking && onCommand)) continue;
        } else if (command.name === "system") {
          if (!(caps.system_prompt && onCommand)) continue;
        }
        items.push(command);
      }
      return items;
    }

    /** The palette is open only for a draft that is exactly `/` plus a word. */
    function commandQuery() {
      if (state.exited) return null;
      const value = input.value;
      if (!/^\/\S*$/.test(value)) return null;
      return value.slice(1).toLowerCase();
    }

    function closePalette() {
      state.palette.items = [];
      state.palette.index = 0;
      paletteEl.hidden = true;
      paletteEl.innerHTML = "";
    }

    function renderPalette() {
      paletteEl.innerHTML = "";
      if (!state.palette.items.length) {
        paletteEl.hidden = true;
        return;
      }
      paletteEl.hidden = false;
      state.palette.items.forEach((command, index) => {
        const item = el(
          "div",
          "pi-command-item agent-palette-item" +
            (index === state.palette.index ? " active" : "")
        );
        item.appendChild(el("span", "pi-command-name agent-palette-name", "/" + command.name));
        if (command.description) {
          item.appendChild(
            el("span", "pi-command-desc agent-palette-desc", command.description)
          );
        }
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          acceptCommand(index);
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
      state.palette.items = items;
      if (state.palette.index >= items.length) state.palette.index = 0;
      renderPalette();
    }

    function movePalette(delta) {
      if (!state.palette.items.length) return;
      const count = state.palette.items.length;
      state.palette.index = (state.palette.index + delta + count) % count;
      renderPalette();
    }

    /** Accepting clears the draft and moves focus to the control the command
     * names; it never sends the draft itself as a prompt. */
    function acceptCommand(index) {
      const command = state.palette.items[index];
      if (!command) return;
      input.value = "";
      closePalette();
      if (command.name === "compact") {
        runCompact();
        return;
      }
      if (command.name === "model") {
        if (typeof modelSelect.focus === "function") modelSelect.focus();
      } else if (command.name === "thinking") {
        if (typeof thinkingSelect.focus === "function") thinkingSelect.focus();
      } else if (command.name === "system") {
        setSystemOpen(!state.systemOpen);
        const target = state.systemOpen ? systemInput : systemButton;
        if (typeof target.focus === "function") target.focus();
      }
      scheduleRender();
    }

    function runCompact() {
      if (!onCommand) return;
      Promise.resolve(onCommand({ type: "compact" })).then(
        () => {
          showNotice("compaction requested");
          scheduleRender();
        },
        (error) => {
          showNotice(error && error.message ? error.message : "could not compact", "error");
        }
      );
    }

    /** Open or close the system-prompt panel; the palette and the button share
     * this so the panel is filled and refreshed the same way both times. */
    function setSystemOpen(open) {
      const allowed = Boolean(state.capabilities.system_prompt && onCommand);
      state.systemOpen = Boolean(open) && allowed;
      if (state.systemOpen) {
        systemInput.value =
          typeof state.meta.system_prompt === "string" ? state.meta.system_prompt : "";
        // Lazy: the library is listed when the panel is first opened, never at
        // session start, and refreshed on every later open.
        refreshPrompts();
      } else {
        closePromptUi();
      }
      scheduleRender();
    }

    function submitSystemPrompt(text) {
      if (!onCommand) return;
      const value = String(text || "");
      systemSave.disabled = true;
      Promise.resolve(onCommand({ type: "set_system_prompt", text: value }))
        .then(
          () => {
            state.meta.system_prompt = value.trim();
            state.systemOpen = false;
            showNotice(
              state.meta.system_prompt ? "system prompt updated" : "system prompt cleared"
            );
          },
          (error) => {
            showNotice(
              error && error.message ? error.message : "could not set the system prompt",
              "error"
            );
          }
        )
        .then(() => {
          systemSave.disabled = state.exited;
          scheduleRender();
        });
    }

    /** The prompt library is optional: without the capability, no library row
     * is shown and no prompt command is ever sent. */
    function promptsEnabled() {
      return Boolean(state.capabilities.prompt_library && onCommand);
    }

    function promptByName(name) {
      if (!name) return null;
      for (const prompt of state.prompts.list) {
        if (prompt && prompt.name === name) return prompt;
      }
      return null;
    }

    function savedPromptName() {
      return promptByName(state.prompts.selected) ? state.prompts.selected : "";
    }

    function disarmPromptDelete() {
      if (state.prompts.armTimer) clearTimeout(state.prompts.armTimer);
      state.prompts.armTimer = null;
      state.prompts.armed = false;
    }

    /** Closing the panel (or losing the capability) drops the armed delete and
     * the inline name field. */
    function closePromptUi() {
      state.prompts.nameOpen = false;
      disarmPromptDelete();
    }

    function renderPromptOptions() {
      promptSelect.innerHTML = "";
      const placeholder = el("option", "", PROMPT_PLACEHOLDER);
      placeholder.value = "";
      promptSelect.appendChild(placeholder);
      for (const prompt of state.prompts.list) {
        const option = el("option", "", prompt.name);
        option.value = prompt.name;
        promptSelect.appendChild(option);
      }
      // A name that vanished from the library (deleted elsewhere) drops out.
      const name = savedPromptName();
      if (state.prompts.armed && !name) disarmPromptDelete();
      state.prompts.selected = name;
      promptSelect.value = name;
      syncControls();
      scheduleRender();
    }

    function refreshPrompts() {
      if (!promptsEnabled()) return Promise.resolve();
      const generation = state.prompts.generation;
      state.prompts.loading = true;
      syncControls();
      return Promise.resolve(onCommand({ type: "list_prompts" })).then(
        (data) => {
          if (generation !== state.prompts.generation) return;
          state.prompts.loading = false;
          const prompts = data && Array.isArray(data.prompts) ? data.prompts : [];
          state.prompts.list = prompts.filter(
            (prompt) => prompt && typeof prompt.name === "string" && prompt.name
          );
          renderPromptOptions();
        },
        (error) => {
          if (generation !== state.prompts.generation) return;
          state.prompts.loading = false;
          showNotice(
            error && error.message ? error.message : "could not list saved prompts",
            "error"
          );
          syncControls();
          scheduleRender();
        }
      );
    }

    function finishPromptCommand() {
      state.prompts.saving = false;
      syncControls();
      scheduleRender();
    }

    /** Saving never applies the prompt: applying stays the panel's Save button. */
    function savePromptToLibrary() {
      if (!promptsEnabled()) return;
      const name = promptNameInput.value.trim();
      const text = systemInput.value;
      state.prompts.saving = true;
      syncControls();
      Promise.resolve(onCommand({ type: "save_prompt", name, text })).then(
        (data) => {
          const reply = data && data.prompt && typeof data.prompt === "object" ? data.prompt : null;
          // Use the normalized name the adapter stored, not what was typed.
          const saved = reply && typeof reply.name === "string" && reply.name
            ? reply.name
            : name;
          state.prompts.selected = saved;
          state.prompts.nameOpen = false;
          showNotice("saved prompt: " + saved);
          return refreshPrompts();
        },
        (error) => {
          // Keep the name field open so a rejected name or text can be fixed.
          showNotice(
            error && error.message ? error.message : "could not save the prompt",
            "error"
          );
        }
      ).then(finishPromptCommand, finishPromptCommand);
    }

    function deletePrompt(name) {
      if (!promptsEnabled()) return;
      state.prompts.saving = true;
      syncControls();
      Promise.resolve(onCommand({ type: "delete_prompt", name })).then(
        (data) => {
          const deleted = !(data && data.deleted === false);
          state.prompts.selected = "";
          showNotice(
            deleted ? "deleted prompt: " + name : "prompt already gone: " + name
          );
          return refreshPrompts();
        },
        (error) => {
          showNotice(
            error && error.message ? error.message : "could not delete the prompt",
            "error"
          );
        }
      ).then(finishPromptCommand, finishPromptCommand);
    }

    modelSelect.addEventListener("change", () => {
      if (!onCommand || !modelsCapability().set) return;
      const model = modelSelect.value;
      const previous = state.meta.model || session.model || "";
      if (!model || model === previous) return;
      modelSelect.disabled = true;
      Promise.resolve(onCommand({ type: "set_model", model })).then(
        () => {
          state.meta.model = model;
          modelSelect.disabled = false;
          showNotice("model: " + model);
          scheduleRender();
        },
        (error) => {
          modelSelect.value = previous;
          modelSelect.disabled = false;
          showNotice(
            error && error.message ? error.message : "could not switch model",
            "error"
          );
          scheduleRender();
        }
      );
    });

    thinkingSelect.addEventListener("change", () => {
      if (!onCommand) return;
      const level = thinkingSelect.value;
      const previous = state.meta.thinking_level;
      if (!level || level === previous) return;
      thinkingSelect.disabled = true;
      Promise.resolve(onCommand({ type: "set_thinking", level })).then(
        () => {
          state.meta.thinking_level = level;
          thinkingSelect.disabled = false;
          showNotice("thinking: " + level);
          scheduleRender();
        },
        (error) => {
          thinkingSelect.value = previous || thinkingLevels()[0] || "";
          thinkingSelect.disabled = false;
          showNotice(
            error && error.message ? error.message : "could not set thinking effort",
            "error"
          );
          scheduleRender();
        }
      );
    });

    systemButton.addEventListener("click", () => setSystemOpen(!state.systemOpen));

    promptSaveButton.addEventListener("click", () => {
      if (!promptsEnabled()) return;
      state.prompts.nameOpen = true;
      promptNameInput.value = savedPromptName();
      syncControls();
      promptNameInput.focus();
    });
    promptCommit.addEventListener("click", () => savePromptToLibrary());
    promptCancel.addEventListener("click", () => {
      state.prompts.nameOpen = false;
      syncControls();
    });
    promptNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        savePromptToLibrary();
      } else if (event.key === "Escape") {
        event.preventDefault();
        state.prompts.nameOpen = false;
        syncControls();
      }
    });

    promptSelect.addEventListener("change", () => {
      disarmPromptDelete();
      const name = promptSelect.value;
      state.prompts.selected = name;
      const prompt = promptByName(name);
      // Picking a prompt only fills the textarea; Save still applies it.
      if (prompt) systemInput.value = prompt.text;
      syncControls();
    });

    promptDeleteButton.addEventListener("click", () => {
      const name = savedPromptName();
      if (!name || !promptsEnabled()) return;
      if (!state.prompts.armed) {
        state.prompts.armed = true;
        if (state.prompts.armTimer) clearTimeout(state.prompts.armTimer);
        state.prompts.armTimer = setTimeout(() => {
          state.prompts.armed = false;
          state.prompts.armTimer = null;
          syncControls();
        }, PROMPT_DELETE_ARM_MS);
        if (typeof state.prompts.armTimer.unref === "function") {
          state.prompts.armTimer.unref();
        }
        syncControls();
        return;
      }
      disarmPromptDelete();
      deletePrompt(name);
    });

    systemSave.addEventListener("click", () => submitSystemPrompt(systemInput.value));
    systemClear.addEventListener("click", () => {
      systemInput.value = "";
      submitSystemPrompt("");
    });
    systemClose.addEventListener("click", () => {
      state.systemOpen = false;
      closePromptUi();
      scheduleRender();
    });

    function updateStatus() {
      const bits = [];
      if (state.exited) bits.push("exited");
      else if (state.busy) bits.push("working…");
      else if (state.meta.streaming) bits.push("working…");
      else if (!state.connected) bits.push("waiting for stream…");
      else bits.push("ready");
      const model = state.meta.model || session.model;
      if (model) bits.push(String(model));
      if (state.meta.thinking_level && state.meta.thinking_level !== "default") {
        bits.push("thinking " + state.meta.thinking_level);
      }
      if (state.usage.tokens) bits.push(state.usage.tokens + " tok");
      if (state.usage.cost > 0) bits.push("$" + Number(state.usage.cost).toFixed(4));
      const queued = state.queues.steering.length + state.queues.followUp.length;
      if (queued) bits.push(queued + " queued");
      statusEl.textContent = bits.join(" · ");
      statusEl.classList.toggle("busy", state.busy);
      statusEl.classList.toggle("exited", state.exited);

      const canPrompt = !state.exited && state.capabilities.prompt !== false;
      input.disabled = !canPrompt;
      sendButton.disabled = !canPrompt;
      const attachments = state.capabilities.attachments;
      if (attach) {
        attach.setEnabled(canPrompt && Boolean(attachments && attachments.images));
      }
      stopButton.hidden = !state.busy || state.capabilities.abort === false;
      metaEl.textContent = model ? String(model) : "";
    }

    /** Rendered entries kept between renders, keyed by entry id. */
    const renderedEntries = new Map();

    /** Per-block UI state (a thinking block's open flag), keyed by entry id and
     * block index so it outlives a rebuild of the transcript. */
    const blockOpen = new Map();

    function blockKind(block) {
      if (!block || typeof block !== "object") return "plain";
      if (block.type === "tool_call") return "tool_call:" + String(block.call_id || "");
      return String(block.type || "unknown");
    }

    function blockSignature(block) {
      if (!block || typeof block !== "object") return JSON.stringify(String(block || ""));
      if (block.type === "tool_call") {
        const status = state.toolState.get(block.call_id) || null;
        return JSON.stringify([
          block.name || "",
          block.args && Object.keys(block.args).length ? block.args : null,
          block.arguments_text || "",
          status ? [status.done, status.isError, status.output] : null,
        ]);
      }
      return JSON.stringify(block);
    }

    function entryKey(entry, index) {
      return entry && entry.id ? String(entry.id) : "index:" + index;
    }

    /** What an entry's DOM depends on structurally; streamed text is excluded so
     * deltas patch the existing nodes instead of rebuilding them. */
    function structureSignature(entry, blocks, editing) {
      const extra = entry.role === "tool" || !blocks.length ? textOfEntry(entry) : "";
      return [
        entry.role || "",
        entry.is_error ? "error" : "",
        entry.optimistic ? "optimistic" : "",
        editing ? "editing" : "",
        editAvailable(entry) ? "editable" : "",
        blocks.map(blockKind).join("|"),
        extra,
      ].join("\u0001");
    }

    function renderToolCard(wrap, block) {
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
    }

    /** Bring one block's DOM up to date in place where the block type allows. */
    function fillBlock(record, block, messageRole) {
      const wrap = record.node;
      if (!block || typeof block !== "object") {
        const text = String(block || "");
        if (record.text === text) return;
        record.text = text;
        wrap.innerHTML = "";
        renderPlain(wrap, text);
        return;
      }
      if (block.type === "text") {
        const text = String(block.text || "");
        if (record.text === text) return;
        record.text = text;
        wrap.innerHTML = "";
        if (messageRole === "assistant") renderAnswer(wrap, text);
        else renderText(wrap, text);
        return;
      }
      if (block.type === "thinking") {
        const text = String(block.text || "");
        if (!record.pre) {
          const details = el("details", "agent-thinking");
          const pre = el("pre", "agent-pre", text);
          details.append(el("summary", "", "Thinking"), pre);
          details.open = blockOpen.get(record.key) === true;
          details.addEventListener("toggle", () => {
            blockOpen.set(record.key, Boolean(details.open));
          });
          wrap.appendChild(details);
          record.pre = pre;
          record.text = text;
          return;
        }
        if (record.text !== text) {
          record.text = text;
          record.pre.textContent = text;
        }
        return;
      }
      if (block.type === "tool_call") {
        const signature = blockSignature(block);
        if (record.sig === signature) return;
        record.sig = signature;
        wrap.innerHTML = "";
        renderToolCard(wrap, block);
        return;
      }
      const signature = blockSignature(block);
      if (record.sig === signature) return;
      record.sig = signature;
      wrap.innerHTML = "";
      if (block.type === "image") {
        wrap.appendChild(el("div", "agent-image", "[image " + (block.mime_type || "") + "]"));
      } else if (block.type === "file") {
        wrap.appendChild(el("div", "agent-file", "[file]"));
      } else {
        renderPre(wrap, JSON.stringify(block, null, 2), "agent-pre");
      }
    }

    function blockRecord(block, messageRole, key) {
      // `text` starts at a sentinel so the first fill always renders.
      const record = {
        node: el("div", "agent-block"),
        text: "\u0000",
        sig: "",
        pre: null,
        key,
      };
      fillBlock(record, block, messageRole);
      return record;
    }

    function buildEntry(entry, blocks, key, signature, isLive) {
      const article = el("article", "pi-msg agent-msg");
      if (typeof entry.id === "string") article.dataset.entryId = entry.id;
      if (entry.role) article.classList.add("role-" + entry.role);
      if (entry.role === "tool") {
        article.classList.add(entry.is_error ? "error" : "ok");
      }
      if (entry.optimistic) article.classList.add("optimistic");
      const head = el("div", "agent-msg-head");
      head.append(el("div", "pi-role", roleLabel(entry)));
      if (editAvailable(entry) && !editingEntry(entry)) {
        const editButton = el("button", "agent-edit", "edit");
        editButton.type = "button";
        editButton.title =
          entry.role === "assistant"
            ? "Edit this answer and drop the messages after it"
            : "Edit this message and run it again";
        editButton.addEventListener("click", () => openEditor(entry));
        head.append(editButton);
      }
      article.append(head);
      const records = [];
      if (editingEntry(entry)) {
        article.append(buildEditor(entry));
      } else if (entry.role === "tool") {
        renderPre(article, textOfEntry(entry), "agent-tool-output");
      } else if (!blocks.length) {
        renderPlain(article, isLive ? "…" : "");
      } else {
        blocks.forEach((block, index) => {
          const record = blockRecord(block, entry.role, key + "#" + index);
          records.push(record);
          article.append(record.node);
        });
      }
      return { node: article, blocks: records, signature };
    }

    /** Rewriting a past turn is capability-gated per role: a user message
     * needs ``edit_message`` (the adapter drops the later entries and runs
     * again), an assistant answer needs ``edit_answer`` (it is rewritten in
     * place and nothing runs). */
    function editAvailable(entry) {
      const caps = state.capabilities || {};
      if (!onCommand || state.exited || !entry || !entry.id || entry.optimistic) {
        return false;
      }
      if (entry.role === "user") return Boolean(caps.edit_message);
      if (entry.role === "assistant") return Boolean(caps.edit_answer);
      return false;
    }

    function editingEntry(entry) {
      return Boolean(entry && entry.id && state.edit.id === entry.id);
    }

    /** Swapping the body for a textarea only touches this entry, so the rest of
     * the transcript keeps its nodes (and any selection). */
    function buildEditor(entry) {
      const editingAnswer = entry.role === "assistant";
      const wrap = el("div", "agent-edit-form");
      const field = el("textarea", "agent-edit-input");
      field.rows = 3;
      field.value = state.edit.draft;
      field.setAttribute("aria-label", editingAnswer ? "Edit answer" : "Edit message");
      field.addEventListener("input", () => {
        state.edit.draft = field.value;
      });
      field.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || event.shiftKey) return;
        event.preventDefault();
        submitEdit(entry);
      });
      const actionsEl = el("div", "agent-edit-actions");
      const save = el("button", "agent-edit-save", editingAnswer ? "Save" : "Save & resend");
      save.type = "button";
      save.disabled = state.edit.saving;
      save.addEventListener("click", () => submitEdit(entry));
      const cancel = el("button", "agent-edit-cancel", "Cancel");
      cancel.type = "button";
      cancel.disabled = state.edit.saving;
      cancel.addEventListener("click", () => closeEditor());
      actionsEl.append(save, cancel);
      wrap.append(field, actionsEl);
      if (typeof field.focus === "function") field.focus();
      if (typeof field.setSelectionRange === "function") {
        const end = field.value.length;
        field.setSelectionRange(end, end);
      }
      return wrap;
    }

    function openEditor(entry) {
      if (!editAvailable(entry)) return;
      state.edit.id = entry.id;
      state.edit.draft = textOfEntry(entry);
      state.edit.saving = false;
      scheduleRender();
    }

    function closeEditor() {
      state.edit.id = null;
      state.edit.draft = "";
      state.edit.saving = false;
      scheduleRender();
    }

    /** The truncation itself arrives as a ``transcript.truncated`` event from
     * the adapter; this only reports the outcome and closes the editor. */
    function submitEdit(entry) {
      if (!onCommand || state.exited || state.edit.saving) return;
      const editingAnswer = entry.role === "assistant";
      if (state.busy || state.meta.streaming) {
        showNotice(
          editingAnswer
            ? "Wait for the current reply before editing an answer."
            : "Wait for the current reply before editing a message.",
          "warning"
        );
        return;
      }
      const text = String(state.edit.draft || "").trim();
      if (!text) {
        showNotice(
          editingAnswer
            ? "An edited answer cannot be empty."
            : "An edited message cannot be empty.",
          "warning"
        );
        return;
      }
      state.edit.saving = true;
      scheduleRender();
      Promise.resolve(
        onCommand({ type: "edit_message", entry_id: entry.id, message: text })
      ).then(
        (data) => {
          const removed = data && typeof data.removed === "number" ? data.removed : 0;
          const dropped = removed > 1 ? removed - 1 : 0;
          const what = editingAnswer ? "answer edited" : "message edited";
          showNotice(
            dropped ? what + "; " + dropped + " later message(s) dropped" : what
          );
        },
        (error) => {
          // Keep the editor open so the rejected text can be corrected.
          showNotice(
            error && error.message
              ? error.message
              : editingAnswer
                ? "could not edit the answer"
                : "could not edit the message",
            "error"
          );
        }
      ).then(() => {
        state.edit.saving = false;
        scheduleRender();
      });
    }

    /** Reuse an entry's nodes when only block contents changed; rebuild when the
     * structure (role, block kinds, optimistic/tool state) actually differs. */
    function renderEntry(entry, isLive, index) {
      const editing = editingEntry(entry);
      const blocks = editing || !Array.isArray(entry.content) ? [] : entry.content;
      const key = entryKey(entry, index);
      const signature = structureSignature(entry, blocks, editing);
      let record = renderedEntries.get(key);
      if (!record || record.signature !== signature) {
        record = buildEntry(entry, blocks, key, signature, isLive);
        renderedEntries.set(key, record);
      } else {
        for (let i = 0; i < blocks.length; i += 1) {
          fillBlock(record.blocks[i], blocks[i], entry.role);
        }
      }
      record.node.classList.toggle("live", Boolean(isLive));
      return record.node;
    }

    /** Keep surviving nodes in place (they carry selection and open state),
     * remove the rest, and only move nodes when the order actually changed. */
    function reconcileChildren(parent, nodes) {
      const wanted = new Set(nodes);
      for (const child of Array.from(parent.children)) {
        if (!wanted.has(child)) child.remove();
      }
      const children = Array.from(parent.children);
      let first = -1;
      for (let i = 0; i < nodes.length; i += 1) {
        if (children[i] !== nodes[i]) {
          first = i;
          break;
        }
      }
      if (first < 0) return;
      // Re-append the tail in order: appending an attached node moves it, and
      // detaching first keeps the swap-in-place behaviour of the DOM shims.
      for (let i = first; i < nodes.length; i += 1) {
        if (nodes[i].parent === parent) nodes[i].remove();
        parent.appendChild(nodes[i]);
      }
    }

    function forgetEntry(key) {
      renderedEntries.delete(key);
      const prefix = key + "#";
      for (const uiKey of Array.from(blockOpen.keys())) {
        if (uiKey.startsWith(prefix)) blockOpen.delete(uiKey);
      }
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

    let historyNode = null;
    let historySignature = "";
    const emptyNode = el("div", "pi-empty", "No messages yet.");

    /** The history row is rebuilt only when its own state changes, so a delta
     * render keeps every transcript node (and its UI state) untouched. */
    function historyNodeFor() {
      const signature = [
        state.history.hasMore ? "more" : "",
        state.history.loading ? "loading" : "",
        state.history.error || "",
        state.exited ? "exited" : "",
      ].join("\u0001");
      if (!historyNode || signature !== historySignature) {
        historySignature = signature;
        historyNode = historyRow();
      }
      return historyNode;
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
      return Promise.resolve(onHistory(before, HISTORY_PAGE_SIZE))
        .then((page) => {
          if (generation !== history.generation) return false;
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
          return true;
        })
        .catch((error) => {
          if (generation !== history.generation) return false;
          history.loading = false;
          history.error =
            error && error.message ? error.message : "could not load earlier messages";
          render();
          return false;
        });
    }

    function render() {
      const atBottom = stickToBottom;
      const anchor = scrollAnchor;
      const previousTop = messagesEl.scrollTop;
      scrollAnchor = null;
      const nodes = [];
      if (state.history.hasMore || state.history.loading || state.history.error) {
        nodes.push(historyNodeFor());
      }
      const kept = new Set();
      if (!state.entries.length && !state.live) {
        nodes.push(emptyNode);
      } else {
        state.entries.forEach((entry, index) => {
          nodes.push(renderEntry(entry, false, index));
          kept.add(entryKey(entry, index));
        });
        if (state.live) {
          const index = state.entries.length;
          nodes.push(renderEntry(state.live, true, index));
          kept.add(entryKey(state.live, index));
        }
      }
      for (const key of Array.from(renderedEntries.keys())) {
        if (!kept.has(key)) forgetEntry(key);
      }
      reconcileChildren(messagesEl, nodes);
      renderDialogs();
      renderQueue();
      syncControls();
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
        case "transcript.truncated": {
          // Everything from ``entry_id`` on left the adapter's transcript, so
          // drop it here too; the adapter re-sends the edited user message or
          // answer as a fresh message.finished.
          const entryId = typeof event.entry_id === "string" ? event.entry_id : "";
          const at = entryId
            ? state.entries.findIndex((entry) => entry && entry.id === entryId)
            : -1;
          if (at >= 0) state.entries = state.entries.slice(0, at);
          if (
            state.edit.id &&
            !state.entries.some((entry) => entry && entry.id === state.edit.id)
          ) {
            state.edit.id = null;
            state.edit.draft = "";
          }
          break;
        }
        case "session.info":
          if (event.model) state.meta.model = event.model;
          if (event.name !== undefined) state.meta.name = event.name;
          if (typeof event.thinking_level === "string") {
            state.meta.thinking_level = event.thinking_level;
          }
          if (typeof event.system_prompt === "string") {
            state.meta.system_prompt = event.system_prompt;
          }
          if (event.title) titleEl.textContent = event.title;
          break;
        case "notice":
          showNotice(event.text || event.code || "notice", event.level);
          break;
        case "queue.updated": {
          const steering = Array.isArray(event.steering) ? event.steering : [];
          const followUp = Array.isArray(event.follow_up)
            ? event.follow_up
            : Array.isArray(event.followUp)
              ? event.followUp
              : [];
          state.queues = { steering: steering.slice(), followUp: followUp.slice() };
          break;
        }
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
      // Entry ids change with the snapshot, so an open editor cannot survive it.
      state.edit.id = null;
      state.edit.draft = "";
      state.edit.saving = false;
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
      // Invalidate in-flight library replies and drop transient library UI.
      state.prompts.generation += 1;
      state.prompts.loading = false;
      state.prompts.saving = false;
      closePromptUi();
      // A snapshot does not carry the queued texts; the next queue.updated does.
      state.queues = { steering: [], followUp: [] };
      closePalette();
      if (state.meta.name) titleEl.textContent = state.meta.name;
      loadModels();
      render();
    }

    function handle(message) {
      if (!message || !message.type) return;
      if (message.type === "agent_resync") {
        resetFromSnapshot(message);
      } else if (message.type === "agent_event") {
        // A replay after (re)subscribing resends retained events; anything at
        // or below the snapshot cursor is already folded into it.
        if (
          typeof message.seq === "number" &&
          state.cursor >= 0 &&
          message.seq <= state.cursor
        ) {
          return;
        }
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
      closePalette();
      if (pathComplete) pathComplete.close();
      const images = attach ? attach.images() : [];
      input.value = "";
      if (attach) attach.clear();
      // A prompt sent during a run must say how to queue it.  The canonical
      // view asks for follow-up by default; Ctrl+Enter requests steering,
      // matching the pi view's gesture.
      const streamingBehavior = state.busy ? behavior || "followUp" : undefined;
      const content = [{ type: "text", text: text }];
      for (const image of images) content.push({ type: "image", ...image });
      state.entries.push({
        id: "local-" + ++state.localCounter,
        role: "user",
        content: content,
        optimistic: true,
      });
      stickToBottom = true;
      scheduleRender();
      const promise = onPrompt(text, streamingBehavior, images);
      if (promise && promise.catch) {
        promise.catch((error) => {
          state.entries = state.entries.filter(
            (entry) => !(entry.optimistic && textOfEntry(entry) === text)
          );
          if (attach) attach.add(images);
          showNotice(error && error.message ? error.message : "prompt failed", "error");
          scheduleRender();
        });
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendPrompt();
    });
    input.addEventListener("input", updatePalette);
    input.addEventListener("click", updatePalette);
    input.addEventListener("blur", () => {
      const timer = setTimeout(closePalette, 0);
      if (timer && typeof timer.unref === "function") timer.unref();
    });
    input.addEventListener("keydown", (event) => {
      if (pathComplete && pathComplete.keydown(event)) return;
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
          acceptCommand(state.palette.index);
          return;
        }
        // Enter with the palette open accepts and never sends the draft.
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          acceptCommand(state.palette.index);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          closePalette();
          return;
        }
      }
      if (event.key === "Escape") {
        event.preventDefault();
        abort();
        return;
      }
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      sendPrompt(event.ctrlKey || event.metaKey ? "steer" : undefined);
    });
    stopButton.addEventListener("click", abort);

    /** Open every collapsed block under a node so a match inside a thinking
     * block or a tool card is visible.  The view's own open flags follow the
     * browser's `toggle` event, so the state survives the next render. */
    function expandCollapsed(node) {
      if (!node || typeof node !== "object") return;
      if (node.tagName === "DETAILS") node.open = true;
      for (const child of node.children || []) expandCollapsed(child);
    }

    function entryNode(entryId) {
      for (const child of messagesEl.children || []) {
        if (child && child.dataset && child.dataset.entryId === entryId) return child;
      }
      return null;
    }

    let findHitTimer = null;

    function revealEntry(entryId) {
      const node = entryNode(entryId);
      if (!node) return;
      expandCollapsed(node);
      if (typeof node.scrollIntoView === "function") {
        node.scrollIntoView({ block: "center" });
      }
      node.classList.add("find-hit");
      if (findHitTimer) clearTimeout(findHitTimer);
      findHitTimer = setTimeout(() => node.classList.remove("find-hit"), 2000);
    }

    const finder = window.WebtermFind
      ? window.WebtermFind.create(findHost, {
          items: () =>
            state.entries.map((entry) => ({
              id: entry && entry.id,
              role: (entry && entry.role) || "message",
              text: textOfEntry(entry),
            })),
          reveal: revealEntry,
          hasMore: () => state.history.hasMore,
          loadEarlier: () => {
            const pending = loadEarlier();
            return pending === undefined ? false : pending;
          },
          focusTarget: () => input,
        })
      : null;
    findButton.hidden = !finder;
    if (finder) findButton.addEventListener("click", () => finder.toggle());
    root.addEventListener("keydown", (event) => {
      if (!finder) return;
      if (!event.ctrlKey && !event.metaKey) return;
      if (event.altKey) return;
      if (String(event.key || "").toLowerCase() !== "f") return;
      event.preventDefault();
      finder.toggle();
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
        state.queues = { steering: [], followUp: [] };
        closePalette();
        updateStatus();
        input.disabled = true;
        sendButton.disabled = true;
        if (pathComplete) pathComplete.close();
        scheduleRender();
      },
      activate: () => {
        if (!state.exited) input.focus();
      },
      destroy: () => {
        clearTimeout(streamTimer);
        if (pathComplete) pathComplete.destroy();
        if (state.noticeTimer) clearTimeout(state.noticeTimer);
        if (state.prompts.armTimer) clearTimeout(state.prompts.armTimer);
        state.prompts.armTimer = null;
        root.remove();
      },
    };
  }

  window.WebtermAgent = { create };
})();
