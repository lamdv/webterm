/* webterm client: xterm.js instances kept in sync with server-side sessions. */
"use strict";

(function () {
  const terminalArea = document.getElementById("terminal-area");
  const sessionsList = document.getElementById("sessions");
  const launchersEl = document.getElementById("launchers");
  const emptyState = document.getElementById("empty-state");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");

  const TERMINAL_THEME = {
    background: "#0f1117",
    foreground: "#c9d1d9",
    cursor: "#4f9cf9",
    cursorAccent: "#0f1117",
    selectionBackground: "#2b3a55",
    black: "#484f58",
    red: "#ff7b72",
    green: "#3fb950",
    yellow: "#d29922",
    blue: "#58a6ff",
    magenta: "#bc8cff",
    cyan: "#39c5cf",
    white: "#b1bac4",
    brightBlack: "#6e7681",
    brightRed: "#ffa198",
    brightGreen: "#56d364",
    brightYellow: "#e3b341",
    brightBlue: "#79c0ff",
    brightMagenta: "#d2a8ff",
    brightCyan: "#56d4dd",
    brightWhite: "#f0f6fc",
  };

  /** @type {Map<string, {term: any, wrapper: HTMLElement}>} */
  const terminals = new Map();
  const subscribed = new Set();
  let activeId = localStorage.getItem("webterm.active") || null;
  let sessionsMeta = [];
  let socket = null;
  let reconnectDelay = 500;
  let closedByPage = false;
  let activateNext = false;
  let restarting = false;
  const userLaunchers = loadUserLaunchers();

  const CONFIG = window.WEBTERM_CONFIG || {};
  const DEFAULT_FONT_FAMILY =
    '"MesloLGS Nerd Font Mono", "MesloLGL Nerd Font Mono", "MesloLGM Nerd Font Mono",' +
    ' "FantasqueSansM Nerd Font Mono", "JetBrainsMono Nerd Font Mono",' +
    ' "FiraCode Nerd Font Mono", "Hack Nerd Font Mono", "CaskaydiaCove Nerd Font Mono",' +
    ' "Symbols Nerd Font Mono", "PowerlineSymbols", "Noto Sans Mono",' +
    ' "DejaVu Sans Mono", monospace';
  const FONT_FAMILY = CONFIG.fontFamily || DEFAULT_FONT_FAMILY;
  const FONT_SIZE = CONFIG.fontSize || 13;

  // ---------------------------------------------------------------- sidebar

  const appEl = document.getElementById("app");
  const sidebarEl = document.getElementById("sidebar");
  const resizerEl = document.getElementById("sidebar-resizer");
  const collapseBtn = document.getElementById("sidebar-collapse");
  const expandBtn = document.getElementById("sidebar-expand");

  const DEFAULT_SIDEBAR_WIDTH = 220;
  const MIN_SIDEBAR_WIDTH = 150;
  const MAX_SIDEBAR_WIDTH = 640;
  let sidebarWidth = DEFAULT_SIDEBAR_WIDTH;

  function maxSidebarWidth() {
    return Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, window.innerWidth - 200));
  }

  function clampSidebarWidth(width) {
    return Math.min(maxSidebarWidth(), Math.max(MIN_SIDEBAR_WIDTH, Math.round(width)));
  }

  function currentSidebarWidth() {
    return sidebarEl.getBoundingClientRect().width;
  }

  function setSidebarWidth(width, persist) {
    const clamped = clampSidebarWidth(width);
    sidebarWidth = clamped;
    sidebarEl.style.setProperty("--sidebar-width", clamped + "px");
    if (persist !== false) {
      try {
        localStorage.setItem("webterm.sidebarWidth", String(clamped));
      } catch (e) {
        /* storage may be unavailable */
      }
    }
    return clamped;
  }

  function loadSidebarWidth() {
    const stored = parseInt(localStorage.getItem("webterm.sidebarWidth"), 10);
    return Number.isFinite(stored) ? clampSidebarWidth(stored) : DEFAULT_SIDEBAR_WIDTH;
  }

  function setSidebarCollapsed(collapsed) {
    appEl.classList.toggle("sidebar-collapsed", collapsed);
    collapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    expandBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    expandBtn.hidden = !collapsed;
    try {
      localStorage.setItem("webterm.sidebarCollapsed", collapsed ? "1" : "0");
    } catch (e) {
      /* storage may be unavailable */
    }
  }

  function isSidebarCollapsed() {
    return appEl.classList.contains("sidebar-collapsed");
  }

  collapseBtn.addEventListener("click", () => setSidebarCollapsed(true));
  expandBtn.addEventListener("click", () => setSidebarCollapsed(false));

  // Drag the divider to resize the sidebar.
  let dragStartX = 0;
  let dragStartWidth = 0;
  resizerEl.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || isSidebarCollapsed()) return;
    dragStartX = event.clientX;
    dragStartWidth = currentSidebarWidth();
    appEl.classList.add("sidebar-resizing");
    resizerEl.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  resizerEl.addEventListener("pointermove", (event) => {
    if (!resizerEl.hasPointerCapture(event.pointerId)) return;
    setSidebarWidth(dragStartWidth + (event.clientX - dragStartX));
  });
  function endResize(event) {
    if (!resizerEl.hasPointerCapture(event.pointerId)) return;
    resizerEl.releasePointerCapture(event.pointerId);
    appEl.classList.remove("sidebar-resizing");
  }
  resizerEl.addEventListener("pointerup", endResize);
  resizerEl.addEventListener("pointercancel", endResize);
  resizerEl.addEventListener("dblclick", () => setSidebarWidth(DEFAULT_SIDEBAR_WIDTH));

  // Keyboard resizing for the focusable separator.
  resizerEl.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 40 : 10;
    if (event.key === "ArrowLeft") {
      setSidebarWidth(currentSidebarWidth() - step);
    } else if (event.key === "ArrowRight") {
      setSidebarWidth(currentSidebarWidth() + step);
    } else if (event.key === "Home") {
      setSidebarWidth(DEFAULT_SIDEBAR_WIDTH);
    } else {
      return;
    }
    event.preventDefault();
  });

  setSidebarWidth(loadSidebarWidth(), false);
  setSidebarCollapsed(localStorage.getItem("webterm.sidebarCollapsed") === "1");

  // Keep a restored width within bounds when the window shrinks.
  window.addEventListener("resize", () => setSidebarWidth(sidebarWidth, false));

  // ------------------------------------------------------------------ socket

  function setStatus(state, text) {
    statusDot.className = "dot " + state;
    statusText.textContent = text;
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${proto}//${location.host}/ws`);
    socket.binaryType = "arraybuffer";

    socket.onopen = () => {
      reconnectDelay = 500;
      restarting = false;
      setStatus("connected", "connected");
      subscribed.clear();
      socket.send(JSON.stringify({ type: "list" }));
    };

    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        handleControl(JSON.parse(event.data));
      } else {
        handleOutput(event.data);
      }
    };

    socket.onclose = () => {
      if (closedByPage) return;
      if (restarting) {
        setStatus("restarting", "restarting…");
      } else {
        setStatus("disconnected", "disconnected — retrying…");
      }
      subscribed.clear();
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 8000);
    };

    socket.onerror = () => socket.close();
  }

  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  }

  // ----------------------------------------------------------- control plane

  function handleControl(message) {
    switch (message.type) {
      case "sessions":
        reconcileSessions(message.sessions || []);
        break;
      case "attached":
        break;
      case "exit":
        handleExit(message.session_id);
        break;
      case "restarting":
        // Broadcast to every client when the server is about to re-exec.
        restarting = true;
        setStatus("restarting", "restarting…");
        break;
      default:
        break;
    }
  }

  function reconcileSessions(sessions) {
    sessionsMeta = sessions;
    const knownBefore = new Set(terminals.keys());
    const seen = new Set();

    for (const session of sessions) {
      seen.add(session.id);
      const entry = ensureTerminal(session);
      if (!subscribed.has(session.id)) {
        subscribed.add(session.id);
        // Resume where this terminal left off.  The server replays only the
        // bytes after that offset, so a reconnect never disturbs terminal
        // state (alternate buffer, mouse tracking, scrollback).
        send({ type: "subscribe", session_id: session.id, since: entry.offset });
      }
    }

    for (const [id, entry] of Array.from(terminals)) {
      if (!seen.has(id)) {
        entry.term.dispose();
        entry.wrapper.remove();
        terminals.delete(id);
        subscribed.delete(id);
      }
    }

    renderTabs();

    // When the user explicitly created a session, switch to the new one so it
    // is laid out and fitted exactly like the first session.
    let newest = null;
    for (const session of sessions) {
      if (!knownBefore.has(session.id)) newest = session.id;
    }

    if (activateNext && newest) {
      activateNext = false;
      activate(newest);
    } else if (!activeId || !seen.has(activeId)) {
      activate(sessions.length ? sessions[0].id : null);
    } else {
      activate(activeId);
    }
  }

  // xterm.js (through at least 5.5.0) silently drops wheel events that its
  // line accumulator rounds down to zero: small/high-resolution wheel and
  // trackpad deltas, and any wheel event while Shift is held.  Two behaviours
  // depend on those events:
  //
  //   * Full-screen applications that requested mouse tracking (pi, btop, ...)
  //     receive scrolling only as SGR mouse reports, so the drops disable
  //     scrolling entirely.
  //   * Full-screen applications without mouse tracking expect "alternate
  //     scroll": xterm translates the wheel into cursor up/down keys, but the
  //     same zero-line early return skips that, so the event escapes to the
  //     browser and scrolls the page instead of reaching the PTY.
  //
  // Handle both from a capture-phase listener.  Returns true when the event was
  // consumed and the DOM default must be suppressed (even if a rounded-to-zero
  // alternate-scroll tick produced no keys yet).
  function handleWheel(term, event, sessionId) {
    if (event.deltaY === 0) return false;
    if (forwardMouseWheel(term, event)) return true;

    // Normal buffer: leave the event to xterm so its scrollback scrolls.
    const buffer = term.buffer && term.buffer.active;
    if (!buffer || buffer.type !== "alternate") return false;

    // Alternate buffer: consume the event so the page never scrolls, and send
    // the cursor-up/down keys xterm would have sent, honouring application
    // cursor keys mode (DECCKM).  getLinesScrolled() accumulates
    // high-resolution deltas across events, so a rounded-to-zero tick simply
    // contributes to the next one instead of escaping to the browser.
    const lines = wheelScrollLines(term, event);
    if (lines > 0) {
      const app = term.modes && term.modes.applicationCursorKeysMode;
      const key = "\x1b" + (app ? "O" : "[") + (event.deltaY < 0 ? "A" : "B");
      send({ type: "input", session_id: sessionId, data: key.repeat(lines) });
    }
    return true;
  }

  // Mouse-tracking applications receive wheel input as SGR mouse reports.
  // xterm emits exactly one report per wheel event, so this matches its
  // behavior for ordinary wheel ticks.
  function forwardMouseWheel(term, event) {
    const core = term._core;
    const modes = term.modes;
    if (!core || !modes || !modes.mouseTrackingMode || modes.mouseTrackingMode === "none") {
      return false;
    }
    const mouseService = core._mouseService;
    const coreMouseService = core.coreMouseService;
    if (
      !core.screenElement ||
      !mouseService ||
      typeof mouseService.getMouseReportCoords !== "function" ||
      !coreMouseService ||
      typeof coreMouseService.triggerMouseEvent !== "function"
    ) {
      return false;
    }
    const coords = mouseService.getMouseReportCoords(event, core.screenElement);
    if (!coords) return false;
    coreMouseService.triggerMouseEvent({
      col: coords.col,
      row: coords.row,
      x: coords.x,
      y: coords.y,
      button: 4, // wheel
      action: event.deltaY < 0 ? 0 : 1, // 0 = up, 1 = down
      ctrl: event.ctrlKey,
      alt: event.altKey,
      shift: event.shiftKey,
    });
    return true;
  }

  // Reuse xterm's line-per-wheel accounting so alternate-scroll feels the same
  // as stock xterm (including its high-resolution delta accumulation).
  function wheelScrollLines(term, event) {
    const viewport = term._core && term._core.viewport;
    if (viewport && typeof viewport.getLinesScrolled === "function") {
      return Math.abs(viewport.getLinesScrolled(event));
    }
    return 1;
  }

  function ensureTerminal(session) {
    if (terminals.has(session.id)) return terminals.get(session.id);

    const wrapper = document.createElement("div");
    wrapper.className = "term-wrapper";
    wrapper.dataset.sessionId = session.id;
    terminalArea.appendChild(wrapper);

    let term;
    // xterm's canvas-based character measurement can fail silently on browsers
    // that lack TextMetrics.fontBoundingBoxAscent/Descent, leaving a 0x0 cell
    // size: the terminal then stays at 80 columns and glyph spacing breaks.
    // Hiding OffscreenCanvas forces xterm to use its reliable DOM-based
    // measurement instead.  This only needs to cover terminal construction and
    // open(), since that is when CharSizeService is created.
    const hadOffscreen = "OffscreenCanvas" in window;
    const savedOffscreen = window.OffscreenCanvas;
    let hidOffscreen = false;
    try {
      if (hadOffscreen) {
        window.OffscreenCanvas = undefined;
        hidOffscreen = window.OffscreenCanvas === undefined;
      }
    } catch (e) {
      hidOffscreen = false;
    }

    try {
      term = new window.Terminal({
        cursorBlink: true,
        fontFamily: FONT_FAMILY,
        fontSize: FONT_SIZE,
        lineHeight: 1.0,
        scrollback: 10000,
        allowProposedApi: true,
        theme: TERMINAL_THEME,
      });
      term.open(wrapper);
    } catch (error) {
      console.error("webterm: failed to create terminal", error);
      wrapper.innerHTML =
        '<div class="term-error">Failed to start terminal renderer.</div>';
      return null;
    } finally {
      if (hidOffscreen) {
        try {
          window.OffscreenCanvas = savedOffscreen;
        } catch (e) {
          /* ignore: measurement already happened */
        }
      }
    }

    term.onData((data) => send({ type: "input", session_id: session.id, data }));
    term.onResize(({ cols, rows }) =>
      send({ type: "resize", session_id: session.id, cols, rows })
    );

    // Capture phase, so the event never reaches xterm's lossy wheel handler
    // when we have already turned it into input for the application.
    wrapper.addEventListener(
      "wheel",
      (event) => {
        if (!handleWheel(term, event, session.id)) return;
        event.preventDefault();
        event.stopImmediatePropagation();
      },
      { capture: true, passive: false }
    );

    const entry = { term, wrapper, offset: 0 };
    terminals.set(session.id, entry);
    return entry;
  }

  function handleOutput(buffer) {
    const view = new Uint8Array(buffer);
    if (view.length < 1) return;
    const idLength = view[0];
    const header = 1 + idLength + 8;
    if (view.length < header) return;
    const id = new TextDecoder().decode(view.subarray(1, 1 + idLength));
    const entry = terminals.get(id);
    if (!entry) return;
    const offset = Number(new DataView(buffer).getBigUint64(1 + idLength));
    applyOutput(entry, offset, view.subarray(header));
  }

  // Write a chunk at its absolute position in the session's output stream,
  // tolerating replay overlap and gaps.  A forward jump means the bytes the
  // terminal missed were evicted from the server's scrollback while it was
  // disconnected, so start over from the retained tail; otherwise the chunk
  // continues exactly where the terminal left off.
  function applyOutput(entry, offset, payload) {
    if (offset > entry.offset) {
      entry.term.reset();
      entry.offset = offset;
    } else if (offset < entry.offset) {
      const skip = entry.offset - offset;
      if (skip >= payload.length) return;
      payload = payload.subarray(skip);
    }
    entry.offset += payload.length;
    entry.term.write(payload);
  }

  function handleExit(sessionId) {
    const entry = terminals.get(sessionId);
    if (entry) {
      entry.term.write("\r\n\x1b[2m[process exited]\x1b[0m\r\n");
    }
    subscribed.delete(sessionId);
  }

  // ------------------------------------------------------------- tab / view

  function renderTabs() {
    sessionsList.innerHTML = "";
    for (const session of sessionsMeta) {
      const tab = document.createElement("li");
      tab.className = "tab";
      tab.dataset.id = session.id;

      const title = document.createElement("span");
      title.className = "tab-title";
      title.textContent = session.title;
      title.title = session.title;

      const close = document.createElement("button");
      close.className = "tab-close";
      close.textContent = "\u00d7";
      close.title = "Close session";
      close.addEventListener("click", (event) => {
        event.stopPropagation();
        send({ type: "kill", session_id: session.id });
      });

      tab.append(title, close);
      tab.addEventListener("click", () => activate(session.id));
      tab.addEventListener("dblclick", () => renameSession(session));
      sessionsList.appendChild(tab);
    }
    updateActiveTab();
    emptyState.hidden = sessionsMeta.length > 0;
  }

  function renameSession(session) {
    const next = window.prompt("Rename session", session.title);
    if (next && next.trim()) {
      send({ type: "rename", session_id: session.id, title: next.trim() });
    }
  }

  function updateActiveTab() {
    for (const tab of sessionsList.children) {
      tab.classList.toggle("active", tab.dataset.id === activeId);
    }
  }

  function activate(id) {
    activeId = id;
    if (id) localStorage.setItem("webterm.active", id);
    for (const [sessionId, entry] of terminals) {
      entry.wrapper.classList.toggle("active", sessionId === id);
    }
    updateActiveTab();
    const entry = id ? terminals.get(id) : null;
    if (entry) {
      requestAnimationFrame(() => {
        fit(entry, 0);
        entry.term.focus();
      });
      // A second pass catches cases where the terminal was only just made
      // visible and its viewport size was not final on the first frame.
      setTimeout(() => fit(entry, 0), 50);
    }
  }

  // ------------------------------------------------------------------ resize

  function cellSize(term, wrapper) {
    // Prefer xterm's own renderer metrics when they are valid.
    const core = term._core;
    const dims = core && core._renderService && core._renderService.dimensions;
    if (
      dims &&
      dims.css &&
      dims.css.cell &&
      dims.css.cell.width > 0 &&
      dims.css.cell.height > 0
    ) {
      return { width: dims.css.cell.width, height: dims.css.cell.height };
    }
    // Otherwise measure a character with the same font directly.
    return domCellSize();
  }

  let measureSpan = null;

  function domCellSize() {
    if (!measureSpan) {
      measureSpan = document.createElement("span");
      measureSpan.setAttribute("aria-hidden", "true");
      measureSpan.style.cssText =
        "position:absolute;top:-9999px;left:-9999px;visibility:hidden;white-space:pre;line-height:1;";
      measureSpan.textContent = "W".repeat(32);
      document.body.appendChild(measureSpan);
    }
    measureSpan.style.fontFamily = FONT_FAMILY;
    measureSpan.style.fontSize = FONT_SIZE + "px";
    const rect = measureSpan.getBoundingClientRect();
    const width = rect.width / 32;
    if (width > 0 && rect.height > 0) {
      return { width: width, height: rect.height };
    }
    return null;
  }

  function fit(entry, attempt) {
    const { term, wrapper } = entry;
    const viewport = wrapper.querySelector(".xterm-viewport");
    const width = (viewport ? viewport.clientWidth : wrapper.clientWidth) || wrapper.clientWidth;
    const height = (viewport ? viewport.clientHeight : wrapper.clientHeight) || wrapper.clientHeight;
    if (!width || !height) return;

    const cell = cellSize(term, wrapper);
    if (!cell) {
      // The renderer has not measured fonts yet; try again shortly.
      if (attempt < 20) setTimeout(() => fit(entry, attempt + 1), 25);
      return;
    }

    // xterm measures a cell, but if the font's real advance is wider than that
    // measurement the rightmost columns get clipped.  Calibrate against the
    // width xterm actually rendered so the last column stays on screen.  Only
    // do this for a visible terminal; hidden ones may not have laid out.
    let cellWidth = cell.width;
    if (wrapper.classList.contains("active")) {
      const rowsEl = wrapper.querySelector(".xterm-rows");
      if (rowsEl && term.cols > 0) {
        const rendered = rowsEl.scrollWidth / term.cols;
        if (rendered > cell.width * 0.5 && rendered < cell.width * 2) {
          cellWidth = rendered;
        }
      }
    }

    const cols = Math.max(2, Math.floor(width / cellWidth));
    const rows = Math.max(1, Math.floor(height / cell.height));
    if (cols !== term.cols || rows !== term.rows) {
      term.resize(cols, rows);
      // Re-measure once xterm has re-rendered at the new width, since the
      // rendered row width can change with the column count.
      if (attempt < 4 && Math.abs(cellWidth - cell.width) > 0.05) {
        setTimeout(() => fit(entry, attempt + 1), 40);
      }
    }

    if (!entry.logged) {
      entry.logged = true;
      console.debug("[webterm] grid", {
        cols: term.cols,
        rows: term.rows,
        viewportWidth: width,
        xtermCellWidth: cell.width,
        renderedCellWidth: cellWidth,
      });
    }
  }

  let resizeTimer = null;
  function refitAll() {
    for (const entry of terminals.values()) fit(entry, 0);
  }

  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(refitAll, 80);
  });

  // Refit whenever the terminal area itself changes size (sidebar, layout,
  // scrollbar appearing, ...), not just on window resize.  Fit every session
  // so background shells keep the right PTY size too.
  if (typeof ResizeObserver !== "undefined") {
    let pending = false;
    const observer = new ResizeObserver(() => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        refitAll();
      });
    });
    observer.observe(terminalArea);
  }

  // ---------------------------------------------------------------- actions

  document.getElementById("new-session").addEventListener("click", openNewSessionPopover);
  document.getElementById("empty-new").addEventListener("click", () => createSession());
  document.getElementById("restart-server").addEventListener("click", () => {
    if (!window.confirm("Restart the server? All sessions will be closed.")) return;
    restarting = true;
    setStatus("restarting", "restarting…");
    send({ type: "restart" });
  });

  function renderLaunchers() {
    if (!launchersEl) return;
    launchersEl.innerHTML = "";

    const add = document.createElement("button");
    add.className = "launcher launcher-add";
    add.textContent = "+";
    add.title = "Add a command to run in a new session";
    add.addEventListener("click", openLauncherPopover);
    launchersEl.appendChild(add);
    addLauncherButton = add;

    for (const launcher of configuredLaunchers()) appendLauncher(launcher, false);
    for (const launcher of userLaunchers) appendLauncher(launcher, true);
  }

  function configuredLaunchers() {
    return (CONFIG.launchers || []).filter((l) => l && isValidCommand(l.command));
  }

  // A launcher command may be an argv array (config launchers) or a plain
  // shell command string (user launchers), which is typed verbatim so shell
  // syntax such as ``&&`` and pipes keeps working.
  function isValidCommand(command) {
    if (typeof command === "string") return command.trim().length > 0;
    return (
      Array.isArray(command) &&
      command.length > 0 &&
      command.every((arg) => typeof arg === "string")
    );
  }

  function commandText(command) {
    return typeof command === "string" ? command : command.join(" ");
  }

  function commandLabel(launcher) {
    if (launcher.label) return launcher.label;
    if (typeof launcher.command === "string") {
      return launcher.command.trim().split(/\s+/)[0] || launcher.command;
    }
    return launcher.command[0];
  }

  function appendLauncher(launcher, removable) {
    if (!launcher || !isValidCommand(launcher.command)) return;
    const pill = document.createElement("button");
    pill.className = removable ? "launcher launcher-user" : "launcher";
    pill.title = commandText(launcher.command);

    const label = document.createElement("span");
    label.className = "launcher-label";
    label.textContent = commandLabel(launcher);
    pill.appendChild(label);
    pill.addEventListener("click", () => createSession(launcher.command, launcher.title));

    if (removable) {
      const remove = document.createElement("span");
      remove.className = "launcher-remove";
      remove.textContent = "\u00d7";
      remove.title = "Remove";
      remove.setAttribute("role", "button");
      remove.setAttribute("aria-label", "Remove " + label.textContent);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        removeLauncher(launcher);
      });
      pill.appendChild(remove);
      // Touch-friendly deletion: long-press, or right-click on desktop.
      attachLongPress(pill, () => removeLauncher(launcher));
    }

    launchersEl.appendChild(pill);
  }

  function removeLauncher(launcher) {
    const index = userLaunchers.indexOf(launcher);
    if (index === -1) return;
    if (!window.confirm('Remove "' + commandLabel(launcher) + '"?')) return;
    userLaunchers.splice(index, 1);
    saveUserLaunchers();
    renderLaunchers();
  }

  // Fire callback on a ~600ms press without movement, or on context-menu.
  function attachLongPress(element, callback) {
    let timer = null;
    let startX = 0;
    let startY = 0;
    let suppressClick = false;
    const cancel = () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };
    element.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      suppressClick = false;
      startX = event.clientX;
      startY = event.clientY;
      timer = setTimeout(() => {
        timer = null;
        suppressClick = true;
        callback();
      }, 600);
    });
    element.addEventListener("pointermove", (event) => {
      if (timer && (Math.abs(event.clientX - startX) > 8 || Math.abs(event.clientY - startY) > 8)) {
        cancel();
      }
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      element.addEventListener(type, cancel);
    }
    element.addEventListener(
      "click",
      (event) => {
        if (suppressClick) {
          suppressClick = false;
          event.stopPropagation();
          event.preventDefault();
        }
      },
      true
    );
    element.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      callback();
    });
  }

  // ------------------------------------------------------------- popovers

  const newSessionPopover = document.getElementById("new-session-popover");
  const newSessionForm = document.getElementById("new-session-form");
  const newSessionNameInput = document.getElementById("new-session-name");
  const newSessionCommandInput = document.getElementById("new-session-command");
  const newSessionButton = document.getElementById("new-session");

  const launcherPopover = document.getElementById("launcher-popover");
  const launcherForm = document.getElementById("launcher-form");
  const launcherCommandInput = document.getElementById("launcher-command");
  const launcherNameInput = document.getElementById("launcher-name");
  let addLauncherButton = null;

  // Show a popover beneath its anchor, flipping to stay inside the viewport.
  function positionPopover(popover, anchor) {
    const rect = anchor.getBoundingClientRect();
    popover.hidden = false;

    // Measure after showing so the popover has its natural size.
    const pop = popover.getBoundingClientRect();
    let left = rect.left;
    let top = rect.bottom + 6;
    if (left + pop.width > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - pop.width - 8);
    }
    if (top + pop.height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - pop.height - 6);
    }
    popover.style.left = left + "px";
    popover.style.top = top + "px";
  }

  function openNewSessionPopover() {
    if (!newSessionPopover) return;
    closeLauncherPopover();
    positionPopover(newSessionPopover, newSessionButton || newSessionPopover.parentElement);
    newSessionNameInput.value = "";
    newSessionCommandInput.value = "";
    newSessionNameInput.focus();
  }

  function closeNewSessionPopover() {
    if (newSessionPopover) newSessionPopover.hidden = true;
  }

  function openLauncherPopover() {
    if (!launcherPopover) return;
    closeNewSessionPopover();
    positionPopover(launcherPopover, addLauncherButton || launchersEl);
    launcherCommandInput.value = "";
    launcherNameInput.value = "";
    launcherCommandInput.focus();
  }

  function closeLauncherPopover() {
    if (launcherPopover) launcherPopover.hidden = true;
  }

  if (newSessionForm) {
    newSessionForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = newSessionNameInput.value.trim();
      const command = newSessionCommandInput.value.trim();
      createSession(command || undefined, name || undefined);
      closeNewSessionPopover();
    });
  }

  const newSessionCancel = document.getElementById("new-session-cancel");
  if (newSessionCancel) newSessionCancel.addEventListener("click", closeNewSessionPopover);

  if (launcherForm) {
    launcherForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const command = launcherCommandInput.value.trim();
      if (!command) return;
      const first = command.split(/\s+/)[0];
      const label = launcherNameInput.value.trim() || first;
      userLaunchers.push({ label: label, command: command });
      saveUserLaunchers();
      renderLaunchers();
      createSession(command, label);
      launchersEl.scrollLeft = 0;
      closeLauncherPopover();
    });
  }

  const launcherCancel = document.getElementById("launcher-cancel");
  if (launcherCancel) launcherCancel.addEventListener("click", closeLauncherPopover);

  document.addEventListener("pointerdown", (event) => {
    const pairs = [
      [newSessionPopover, newSessionButton],
      [launcherPopover, addLauncherButton],
    ];
    for (const [pop, anchor] of pairs) {
      if (!pop || pop.hidden) continue;
      if (pop.contains(event.target)) continue;
      if (anchor && anchor.contains(event.target)) continue;
      pop.hidden = true;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (newSessionPopover) newSessionPopover.hidden = true;
    if (launcherPopover) launcherPopover.hidden = true;
  });

  function saveUserLaunchers() {
    try {
      localStorage.setItem("webterm.launchers", JSON.stringify(userLaunchers));
    } catch (e) {
      /* storage may be unavailable; keep them for this session only */
    }
  }

  function loadUserLaunchers() {
    try {
      const stored = JSON.parse(localStorage.getItem("webterm.launchers") || "[]");
      if (Array.isArray(stored)) {
        return stored.filter((l) => l && isValidCommand(l.command));
      }
    } catch (e) {
      /* ignore malformed storage */
    }
    return [];
  }

  function createSession(command, title) {
    const entry = activeId ? terminals.get(activeId) : null;
    const cols = entry ? entry.term.cols : 80;
    const rows = entry ? entry.term.rows : 24;
    activateNext = true;
    const message = { type: "create", cols, rows };
    if (typeof command === "string" && command.trim()) message.command = command;
    else if (Array.isArray(command) && command.length) message.command = command;
    if (title) message.title = title;
    send(message);
  }

  window.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.shiftKey && (event.key === "N" || event.key === "n")) {
      event.preventDefault();
      openNewSessionPopover();
    } else if (event.ctrlKey && event.shiftKey && (event.key === "B" || event.key === "b")) {
      event.preventDefault();
      setSidebarCollapsed(!isSidebarCollapsed());
    }
  });

  window.addEventListener("beforeunload", () => {
    closedByPage = true;
    if (socket) socket.close();
  });

  // ------------------------------------------------------------------- boot

  if (!window.Terminal) {
    document.body.innerHTML =
      '<p style="padding:2rem;font-family:sans-serif">Failed to load xterm.js.</p>';
    return;
  }

  setStatus("", "connecting…");
  renderLaunchers();
  connect();

  // Fonts can change metrics after first paint; refit once they are ready.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      for (const entry of terminals.values()) fit(entry, 0);
    });
  }
})();
