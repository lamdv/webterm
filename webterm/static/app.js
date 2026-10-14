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
  let everConnected = false;
  let closedByPage = false;
  let activateNext = false;
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
      setStatus("connected", "connected");
      subscribed.clear();
      if (everConnected) {
        // A fresh connection replays full history; clear stale scrollback so
        // it is not duplicated.
        for (const { term } of terminals.values()) term.reset();
      }
      everConnected = true;
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
      setStatus("disconnected", "disconnected — retrying…");
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
      ensureTerminal(session);
      if (!subscribed.has(session.id)) {
        subscribed.add(session.id);
        send({ type: "subscribe", session_id: session.id });
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

    const entry = { term, wrapper };
    terminals.set(session.id, entry);
    return entry;
  }

  function handleOutput(buffer) {
    const view = new Uint8Array(buffer);
    if (view.length < 1) return;
    const idLength = view[0];
    if (view.length < 1 + idLength) return;
    const id = new TextDecoder().decode(view.subarray(1, 1 + idLength));
    const payload = view.subarray(1 + idLength);
    const entry = terminals.get(id);
    if (entry) entry.term.write(payload);
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

  document.getElementById("new-session").addEventListener("click", () => createSession());
  document.getElementById("empty-new").addEventListener("click", () => createSession());

  function renderLaunchers() {
    if (!launchersEl) return;
    launchersEl.innerHTML = "";

    const add = document.createElement("button");
    add.className = "launcher launcher-add";
    add.textContent = "+";
    add.title = "Add a command to run in a new session";
    add.addEventListener("click", promptForCommand);
    launchersEl.appendChild(add);

    for (const launcher of configuredLaunchers()) appendLauncher(launcher, false);
    for (const launcher of userLaunchers) appendLauncher(launcher, true);
  }

  function configuredLaunchers() {
    return (CONFIG.launchers || []).filter(
      (l) => l && Array.isArray(l.command) && l.command.length
    );
  }

  function appendLauncher(launcher, removable) {
    if (!launcher || !Array.isArray(launcher.command) || !launcher.command.length) return;
    const pill = document.createElement("button");
    pill.className = removable ? "launcher launcher-user" : "launcher";
    pill.title = launcher.command.join(" ");

    const label = document.createElement("span");
    label.className = "launcher-label";
    label.textContent = launcher.label || launcher.command[0];
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
    if (!window.confirm('Remove "' + (launcher.label || launcher.command[0]) + '"?')) return;
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

  function promptForCommand() {
    const input = window.prompt("Command to run in a new session:");
    if (!input || !input.trim()) return;
    const command = splitCommand(input.trim());
    if (!command.length) return;
    const name = window.prompt("Pill name (optional):", command[0]);
    const label = (name && name.trim()) || command[0];
    userLaunchers.push({ label: label, command: command });
    saveUserLaunchers();
    renderLaunchers();
    createSession(command, label);
    launchersEl.scrollLeft = 0;
  }

  // Split a command line into argv, honoring single/double quotes and \ escapes.
  function splitCommand(input) {
    const tokens = [];
    let current = "";
    let quote = null;
    let started = false;
    for (let i = 0; i < input.length; i++) {
      const ch = input[i];
      if (quote) {
        if (ch === quote) quote = null;
        else if (ch === "\\" && quote === '"' && i + 1 < input.length) current += input[++i];
        else current += ch;
      } else if (ch === "'" || ch === '"') {
        quote = ch;
        started = true;
      } else if (ch === "\\" && i + 1 < input.length) {
        current += input[++i];
        started = true;
      } else if (/\s/.test(ch)) {
        if (started || current) {
          tokens.push(current);
          current = "";
          started = false;
        }
      } else {
        current += ch;
        started = true;
      }
    }
    if (started || current) tokens.push(current);
    return tokens;
  }

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
        return stored.filter(
          (l) =>
            l &&
            Array.isArray(l.command) &&
            l.command.length &&
            l.command.every((arg) => typeof arg === "string")
        );
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
    if (Array.isArray(command) && command.length) message.command = command;
    if (title) message.title = title;
    send(message);
  }

  window.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.shiftKey && (event.key === "N" || event.key === "n")) {
      event.preventDefault();
      createSession();
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
