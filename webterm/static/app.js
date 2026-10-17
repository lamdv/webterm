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
  const piViews = new Map();
  const subscribed = new Set();
  let activeId = localStorage.getItem("webterm.active") || null;
  let sessionsMeta = [];
  let socket = null;
  let reconnectDelay = 500;
  let closedByPage = false;
  let activateNext = false;
  let restarting = false;
  let editingTabId = null;
  const userLaunchers = loadUserLaunchers();
  const pendingRequests = new Map();
  let requestCounter = 0;

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
  // Below this width the sidebar starts collapsed and hides itself again
  // whenever a shell is chosen.
  const NARROW_VIEWPORT = 640;
  let sidebarWidth = DEFAULT_SIDEBAR_WIDTH;
  let sidebarCollapsedByUser = false;

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

  // The desktop picker passes its sidebar width through the URL so the two
  // views line up.  Returns null when absent.
  function sidebarWidthFromUrl() {
    const raw = new URLSearchParams(window.location.search).get("sidebar");
    if (raw === null) return null;
    const parsed = parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function stripQueryParameter(name) {
    const params = new URLSearchParams(window.location.search);
    if (!params.has(name)) return;
    params.delete(name);
    const search = params.toString();
    window.history.replaceState(
      null,
      "",
      window.location.pathname + (search ? "?" + search : "") + window.location.hash,
    );
  }

  function applySidebarCollapsed(collapsed) {
    appEl.classList.toggle("sidebar-collapsed", collapsed);
    collapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    expandBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    expandBtn.hidden = !collapsed;
  }

  function setSidebarCollapsed(collapsed, persist = true) {
    applySidebarCollapsed(collapsed);
    if (!persist) return;
    sidebarCollapsedByUser = collapsed;
    try {
      localStorage.setItem("webterm.sidebarCollapsed", collapsed ? "1" : "0");
    } catch (e) {
      /* storage may be unavailable */
    }
  }

  function isNarrow() {
    return window.innerWidth < NARROW_VIEWPORT;
  }

  // A narrow viewport hides the sidebar whatever the stored preference says;
  // widening it again restores that preference.
  function refreshSidebarForViewport() {
    applySidebarCollapsed(isNarrow() || sidebarCollapsedByUser);
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

  const requestedWidth = sidebarWidthFromUrl();
  if (requestedWidth !== null) {
    // The desktop picker handed its sidebar width over in the URL; adopt it and
    // drop the parameter so a reload falls back to the stored value.
    setSidebarWidth(requestedWidth);
    stripQueryParameter("sidebar");
  } else {
    setSidebarWidth(loadSidebarWidth(), false);
  }
  sidebarCollapsedByUser = localStorage.getItem("webterm.sidebarCollapsed") === "1";
  refreshSidebarForViewport();

  // Keep a restored width within bounds when the window shrinks, and let a
  // narrow viewport take over the collapsed state.
  window.addEventListener("resize", () => {
    setSidebarWidth(sidebarWidth, false);
    refreshSidebarForViewport();
  });

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

  function request(message, timeout = 30000) {
    return new Promise((resolve, reject) => {
      const id = "ui-" + ++requestCounter;
      const timer = setTimeout(() => {
        pendingRequests.delete(id);
        reject(new Error("request timed out"));
      }, timeout);
      pendingRequests.set(id, { resolve, reject, timer });
      send({ ...message, id });
    });
  }

  function sendPiCommand(sessionId, command) {
    return request({ type: "pi", session_id: sessionId, command }).then((result) => {
      const response = result && result.pi_response;
      if (response && response.success === false) {
        throw new Error(response.error || "pi command failed");
      }
      return response;
    });
  }

  function sendPiUiResponse(sessionId, requestId, response) {
    return request({
      type: "pi_ui_response",
      session_id: sessionId,
      request_id: requestId,
      response,
    });
  }

  // ----------------------------------------------------------- control plane

  function handleControl(message) {
    switch (message.type) {
      case "sessions":
        reconcileSessions(message.sessions || []);
        break;
      case "attached":
        break;
      case "pi_resync":
      case "pi_event":
      case "pi_ui_request":
      case "pi_ui_resolved": {
        const entry = piViews.get(message.session_id);
        if (entry && entry.view) entry.view.handle(message);
        break;
      }
      case "exit":
        handleExit(message.session_id);
        break;
      case "result": {
        const pending = pendingRequests.get(message.id);
        if (pending) {
          pendingRequests.delete(message.id);
          clearTimeout(pending.timer);
          pending.resolve(message.result || {});
        }
        break;
      }
      case "error": {
        const pending = pendingRequests.get(message.id);
        if (pending) {
          pendingRequests.delete(message.id);
          clearTimeout(pending.timer);
          const error = message.error || {};
          pending.reject(new Error(error.message || "request failed"));
        }
        break;
      }
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
    const knownBefore = new Set([...terminals.keys(), ...piViews.keys()]);
    const seen = new Set();

    for (const session of sessions) {
      seen.add(session.id);
      if (session.kind === "pi") {
        ensurePiView(session);
        if (!subscribed.has(session.id)) {
          subscribed.add(session.id);
          send({ type: "subscribe", session_id: session.id });
        }
        continue;
      }
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
    for (const [id, entry] of Array.from(piViews)) {
      if (!seen.has(id)) {
        if (entry.view) entry.view.destroy();
        entry.wrapper.remove();
        piViews.delete(id);
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
    if (lines > 0) sendCursorScroll(term, sessionId, event.deltaY < 0, lines);
    return true;
  }

  // xterm.js 5.5 skips its entire touch handler while the running program has
  // mouse tracking on (`if (coreMouseService.areMouseEventsActive) return`),
  // and its touch scroll only moves the viewport's `scrollTop`, which an
  // alternate buffer has no scrollback for.  Full-screen TUIs (pi, Claude
  // Code, btop, ...) therefore cannot be scrolled with a finger at all.  Turn
  // a vertical drag into the same wheel reports (or alternate-scroll keys) a
  // mouse wheel would have produced, one line per cell of travel.
  function attachDragScroll(term, wrapper, sessionId) {
    const ENGAGE_PX = 10; // vertical travel before a touch becomes a scroll
    let tracking = false; // touchstart seen, direction not yet decided
    let scrolling = false; // gesture committed to scrolling
    let startX = 0;
    let startY = 0;
    let lastY = 0;
    let pending = 0; // signed pixels not yet emitted as whole lines
    let step = 16; // pixels of drag that equal one scrolled line

    // Only the cases xterm cannot handle.  A normal buffer without mouse
    // tracking keeps xterm's own (working) touch scrolling.
    const draggable = () => {
      const buffer = term.buffer && term.buffer.active;
      return mouseTracking(term) || (buffer && buffer.type === "alternate");
    };

    const reset = () => {
      tracking = false;
      scrolling = false;
      pending = 0;
    };

    wrapper.addEventListener(
      "touchstart",
      (event) => {
        if (event.touches.length !== 1 || !draggable()) return;
        const touch = event.touches[0];
        tracking = true;
        scrolling = false;
        startX = touch.clientX;
        startY = touch.clientY;
        lastY = touch.clientY;
        pending = 0;
      },
      { capture: true, passive: true }
    );

    wrapper.addEventListener(
      "touchmove",
      (event) => {
        if (!tracking) return;
        if (event.touches.length !== 1) {
          reset();
          return;
        }
        const touch = event.touches[0];
        if (!scrolling) {
          const dx = touch.clientX - startX;
          const dy = touch.clientY - startY;
          // Mostly horizontal: leave it to the browser / application.
          if (Math.abs(dy) < Math.abs(dx)) {
            if (Math.abs(dx) > ENGAGE_PX) reset();
            return;
          }
          if (Math.abs(dy) < ENGAGE_PX) return;
          scrolling = true;
          const cell = cellSize(term, wrapper);
          step = Math.max(8, cell ? Math.round(cell.height) : 16);
          // Drop the dead-zone travel instead of replaying it as a jump.
          lastY = startY + (dy > 0 ? ENGAGE_PX : -ENGAGE_PX);
          pending = 0;
        }
        // We own the gesture now: no page pan and no xterm handler.
        event.preventDefault();
        event.stopPropagation();
        pending += touch.clientY - lastY;
        lastY = touch.clientY;
        flush(touch.clientX, touch.clientY);
      },
      { capture: true, passive: false }
    );

    wrapper.addEventListener("touchend", reset, { capture: true });
    wrapper.addEventListener("touchcancel", reset, { capture: true });

    function flush(clientX, clientY) {
      let up = 0;
      let down = 0;
      while (pending >= step) {
        pending -= step;
        up++;
      }
      while (pending <= -step) {
        pending += step;
        down++;
      }
      if (up) scrollBy(term, sessionId, true, up, clientX, clientY);
      if (down) scrollBy(term, sessionId, false, down, clientX, clientY);
    }
  }

  // Route a scroll step to whatever the running program understands: SGR wheel
  // reports when it tracks the mouse, cursor keys for an alternate-buffer app
  // without tracking, and the local scrollback otherwise (e.g. a buffer change
  // mid-drag).  Direction: `up` shows older content, matching a wheel-up.
  function scrollBy(term, sessionId, up, lines, clientX, clientY) {
    if (mouseTracking(term)) {
      for (let i = 0; i < lines; i++) sendMouseWheel(term, up, clientX, clientY);
    } else if (term.buffer && term.buffer.active && term.buffer.active.type === "alternate") {
      sendCursorScroll(term, sessionId, up, lines);
    } else if (typeof term.scrollLines === "function") {
      for (let i = 0; i < lines; i++) term.scrollLines(up ? -1 : 1);
    }
  }

  function mouseTracking(term) {
    const modes = term.modes;
    return !!(modes && modes.mouseTrackingMode && modes.mouseTrackingMode !== "none");
  }

  // Mouse-tracking applications receive wheel input as SGR mouse reports.
  // xterm emits exactly one report per wheel event, so this matches its
  // behavior for ordinary wheel ticks.
  function forwardMouseWheel(term, event) {
    if (!mouseTracking(term)) return false;
    return sendMouseWheel(term, event.deltaY < 0, event.clientX, event.clientY, event);
  }

  function sendMouseWheel(term, up, clientX, clientY, modifiers) {
    const core = term._core;
    const mouseService = core && core._mouseService;
    const coreMouseService = core && core.coreMouseService;
    if (
      !core ||
      !core.screenElement ||
      !mouseService ||
      typeof mouseService.getMouseReportCoords !== "function" ||
      !coreMouseService ||
      typeof coreMouseService.triggerMouseEvent !== "function"
    ) {
      return false;
    }
    const coords = mouseService.getMouseReportCoords({ clientX, clientY }, core.screenElement);
    if (!coords) return false;
    coreMouseService.triggerMouseEvent({
      col: coords.col,
      row: coords.row,
      x: coords.x,
      y: coords.y,
      button: 4, // wheel
      action: up ? 0 : 1, // 0 = up, 1 = down
      ctrl: !!(modifiers && modifiers.ctrlKey),
      alt: !!(modifiers && modifiers.altKey),
      shift: !!(modifiers && modifiers.shiftKey),
    });
    return true;
  }

  // Alternate-scroll cursor keys, honouring DECCKM (application cursor keys).
  function sendCursorScroll(term, sessionId, up, lines) {
    const app = term.modes && term.modes.applicationCursorKeysMode;
    const key = "\x1b" + (app ? "O" : "[") + (up ? "A" : "B");
    send({ type: "input", session_id: sessionId, data: key.repeat(lines) });
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

  function ensurePiView(session) {
    let entry = piViews.get(session.id);
    if (entry) return entry;

    const wrapper = document.createElement("div");
    wrapper.className = "pi-wrapper";
    wrapper.dataset.sessionId = session.id;
    terminalArea.appendChild(wrapper);

    let view = null;
    if (window.WebtermPi) {
      view = window.WebtermPi.create(session, {
        onCommand: (command) => sendPiCommand(session.id, command),
        onRefresh: () => request({ type: "pi_snapshot", session_id: session.id }),
        onExport: (format) =>
          request({ type: "pi_export", session_id: session.id, format }),
        onUiResponse: (requestId, response) =>
          sendPiUiResponse(session.id, requestId, response),
      });
      wrapper.appendChild(view.element);
    } else {
      wrapper.textContent = "Failed to load the pi view.";
    }

    entry = { wrapper, view };
    piViews.set(session.id, entry);
    return entry;
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

    // A mouse-tracking full-screen app gets no touch scrolling from xterm, so
    // translate finger drags into the wheel input it does understand.
    attachDragScroll(term, wrapper, session.id);

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
    const piEntry = piViews.get(sessionId);
    if (piEntry) {
      if (piEntry.view) piEntry.view.setExited();
      subscribed.delete(sessionId);
      return;
    }
    const entry = terminals.get(sessionId);
    if (entry) {
      entry.term.write("\r\n\x1b[2m[process exited]\x1b[0m\r\n");
    }
    subscribed.delete(sessionId);
  }

  // ------------------------------------------------------------- tab / view

  // Order sessions as a forest by parent_id.  A session whose parent is gone
  // (or hidden from this client) is treated as a root, so nothing disappears.
  function orderedSessions() {
    const byId = new Map(sessionsMeta.map((session) => [session.id, session]));
    const children = new Map();
    const roots = [];
    for (const session of sessionsMeta) {
      const parent = session.parent_id;
      if (parent && byId.has(parent)) {
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(session);
      } else {
        roots.push(session);
      }
    }
    const ordered = [];
    const walk = (list, depth) => {
      for (const session of list) {
        ordered.push({ session, depth });
        const kids = children.get(session.id);
        if (kids) walk(kids, depth + 1);
      }
    };
    walk(roots, 0);
    return ordered;
  }

  function renderTabs() {
    // Don't rebuild the list under an in-progress inline rename.
    if (editingTabId !== null) return;
    sessionsList.innerHTML = "";
    for (const { session, depth } of orderedSessions()) {
      const tab = document.createElement("li");
      tab.className = "tab";
      if (depth > 0) tab.classList.add("nested");
      if (session.kind === "pi") tab.classList.add("pi");
      if (session.role === "agent") tab.classList.add("agent");
      if (!session.alive) tab.classList.add("exited");
      tab.dataset.id = session.id;
      tab.style.setProperty("--depth", String(depth));

      if (depth > 0) {
        const branch = document.createElement("span");
        branch.className = "tab-branch";
        branch.textContent = "\u2514";
        tab.appendChild(branch);
      }

      const title = document.createElement("span");
      title.className = "tab-title";
      title.textContent = session.title;
      title.title = session.title;
      tab.appendChild(title);

      if (session.role === "agent") {
        const badge = document.createElement("span");
        badge.className = "tab-badge";
        badge.textContent = session.agent || "agent";
        badge.title = session.task || "agent session";
        tab.appendChild(badge);
      }

      if (session.needs_input) {
        const needs = document.createElement("span");
        needs.className = "tab-needs-input";
        needs.textContent = "!";
        needs.title = "Extension is waiting for input";
        tab.appendChild(needs);
      }

      if (!session.alive) {
        const status = document.createElement("span");
        status.className = "tab-exit";
        status.textContent =
          session.exit_code === null ? "exited" : `exit ${session.exit_code}`;
        tab.appendChild(status);
      }

      const close = document.createElement("button");
      close.className = "tab-close";
      close.textContent = "\u00d7";
      close.title = "Close session";
      close.addEventListener("click", (event) => {
        event.stopPropagation();
        send({ type: "kill", session_id: session.id });
      });
      tab.appendChild(close);

      tab.addEventListener("click", () => activate(session.id));
      tab.addEventListener("dblclick", () => startRename(tab, title, session));
      // Touch-friendly rename: long-press the tab, matching the create button.
      // The close button keeps its own tap behaviour.
      attachLongPress(
        tab,
        () => startRename(tab, title, session),
        (event) => !(event.target instanceof Element) || !event.target.closest(".tab-close")
      );
      sessionsList.appendChild(tab);
    }
    updateActiveTab();
    emptyState.hidden = sessionsMeta.length > 0;
  }

  // Double-clicking a tab swaps its label for an inline text input; Enter or
  // blur commits the new name, Escape cancels.
  function startRename(tab, title, session) {
    if (editingTabId !== null) return;
    editingTabId = session.id;

    const input = document.createElement("input");
    input.className = "tab-rename";
    input.type = "text";
    input.value = session.title;
    input.setAttribute("aria-label", "Rename session");

    const finish = (commit) => {
      if (editingTabId === null) return;
      editingTabId = null;
      const value = input.value.trim();
      if (input.parentNode === tab) input.replaceWith(title);
      if (commit && value && value !== session.title) {
        session.title = value;
        send({ type: "rename", session_id: session.id, title: value });
      }
      renderTabs();
    };

    input.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("dblclick", (event) => event.stopPropagation());

    tab.replaceChild(input, title);
    input.focus();
    input.select();
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
    for (const [sessionId, entry] of piViews) {
      entry.wrapper.classList.toggle("active", sessionId === id);
    }
    updateActiveTab();
    // On a narrow viewport, choosing a shell gets the switcher out of the way.
    if (isNarrow()) {
      applySidebarCollapsed(true);
    }
    const piEntry = id ? piViews.get(id) : null;
    if (piEntry && piEntry.view) {
      requestAnimationFrame(() => {
        if (editingTabId !== null) return;
        piEntry.view.activate();
      });
      return;
    }
    const entry = id ? terminals.get(id) : null;
    if (entry) {
      requestAnimationFrame(() => {
        // Don't steal focus from an in-progress tab rename.
        if (editingTabId !== null) return;
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

  // ------------------------------------------------- virtual keyboard

  // Mobile browsers keep the *layout* viewport at full height when the virtual
  // keyboard opens and only shrink the *visual* viewport (visualViewport), so a
  // `100vh` app leaves the terminal's last rows hidden behind the keyboard.
  // Keep `--app-height` in sync with the visual viewport instead, and when the
  // keyboard clearly covers part of the screen, collapse the sidebar so the
  // terminal gets the full width (restoring it when the keyboard closes).
  const KEYBOARD_MIN_INSET = 120; // px the keyboard must cover to count as open
  let keyboardOpen = false;
  let autoCollapsedSidebar = false;

  function viewportInset() {
    const vv = window.visualViewport;
    if (!vv) return 0;
    return Math.max(0, window.innerHeight - (vv.height + vv.offsetTop));
  }

  function syncViewport() {
    const vv = window.visualViewport;
    const height = vv ? vv.height : window.innerHeight;
    const offsetTop = vv ? vv.offsetTop : 0;
    const root = document.documentElement;
    root.style.setProperty("--app-height", Math.round(height) + "px");
    root.style.setProperty("--viewport-offset-top", Math.round(offsetTop) + "px");

    const open = vv ? viewportInset() >= KEYBOARD_MIN_INSET : false;
    if (open === keyboardOpen) return;
    keyboardOpen = open;
    appEl.classList.toggle("keyboard-open", open);

    if (open) {
      // Only take over a sidebar the user had open, and remember that we did.
      if (!isSidebarCollapsed()) {
        autoCollapsedSidebar = true;
        setSidebarCollapsed(true, false);
      }
    } else if (autoCollapsedSidebar) {
      autoCollapsedSidebar = false;
      // If the user re-opened the sidebar while typing, leave it alone.  On a
      // narrow viewport the sidebar stays hidden, as it does everywhere else.
      if (isSidebarCollapsed()) refreshSidebarForViewport();
    }
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
      syncViewport();
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(refitAll, 80);
    });
    window.visualViewport.addEventListener("scroll", syncViewport);
  }
  syncViewport();

  // ---------------------------------------------------------------- actions

  const newSessionButton = document.getElementById("new-session");
  newSessionButton.addEventListener("click", () => {
    closeNewSessionPopover();
    createSession();
  });
  // Long-press (touch) or right-click opens a popover to name the shell and
  // type a precommand into it; a plain click still starts a shell immediately.
  attachLongPress(newSessionButton, openNewSessionPopover);
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

    if (CONFIG.pi && CONFIG.pi.available) appendPiLauncher();
    if (CONFIG.pi && CONFIG.pi.available && CONFIG.pi.sessionDir) appendPiResumeLauncher();

    for (const launcher of configuredLaunchers()) appendLauncher(launcher, false);
    for (const launcher of userLaunchers) appendLauncher(launcher, true);
  }

  function appendPiLauncher() {
    const pill = document.createElement("button");
    pill.className = "launcher launcher-pi";
    pill.textContent = "pi chat";
    pill.title = "Start a structured pi session (RPC)";
    pill.addEventListener("click", createPiSession);
    launchersEl.appendChild(pill);
  }

  function appendPiResumeLauncher() {
    const pill = document.createElement("button");
    pill.className = "launcher launcher-pi launcher-pi-resume";
    pill.textContent = "pi resume";
    pill.title = "Resume a persisted pi session";
    pill.addEventListener("click", createPiResumeSession);
    launchersEl.appendChild(pill);
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
    pill.addEventListener("click", () => createSession(launcher.command, commandLabel(launcher)));

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
  // An optional `shouldStart(event)` predicate can veto a press by target.
  function attachLongPress(element, callback, shouldStart) {
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
    const allowed = (event) => !shouldStart || shouldStart(event);
    element.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      if (!allowed(event)) return;
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
      if (suppressClick) {
        suppressClick = false;
        event.preventDefault();
        return;
      }
      if (!allowed(event)) return;
      event.preventDefault();
      callback();
    });
  }

  // ------------------------------------------------------------- popovers

  const launcherPopover = document.getElementById("launcher-popover");
  const launcherForm = document.getElementById("launcher-form");
  const launcherCommandInput = document.getElementById("launcher-command");
  const launcherNameInput = document.getElementById("launcher-name");
  const newSessionPopover = document.getElementById("new-session-popover");
  const newSessionForm = document.getElementById("new-session-form");
  const newSessionNameInput = document.getElementById("new-session-name");
  const newSessionCommandInput = document.getElementById("new-session-command");
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

  function formatPiSessionTime(entry) {
    const raw = entry.timestamp || (entry.mtime ? entry.mtime * 1000 : null);
    if (!raw) return "";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return String(raw);
    return date.toLocaleString();
  }

  function showPiResumeDialog(onChoose) {
    const overlay = document.createElement("div");
    overlay.className = "pi-trust-overlay";
    const dialog = document.createElement("div");
    dialog.className = "pi-trust-dialog pi-resume-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "Resume a pi session");

    const heading = document.createElement("h2");
    heading.textContent = "Resume pi";
    const copy = document.createElement("p");
    copy.textContent = "Choose a saved session from the webterm-managed pi session directory.";
    const rootLine = document.createElement("div");
    rootLine.className = "pi-resume-root";
    rootLine.textContent = CONFIG.pi.sessionDir || "";
    const list = document.createElement("div");
    list.className = "pi-trust-browser pi-session-list";
    list.textContent = "Loading…";

    const actions = document.createElement("div");
    actions.className = "pi-trust-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    actions.appendChild(cancel);
    dialog.append(heading, copy, rootLine, list, actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    let settled = false;
    let entries = [];

    function finish(entry) {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      onChoose(entry);
    }

    function renderList() {
      list.replaceChildren();
      if (!entries.length) {
        const empty = document.createElement("div");
        empty.className = "pi-trust-empty";
        empty.textContent = "No saved pi sessions yet.";
        list.appendChild(empty);
        return;
      }
      for (const entry of entries) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "pi-session-item";
        const title = document.createElement("span");
        title.className = "pi-session-title";
        const cwd = entry.cwd || "";
        const base = cwd.split("/").filter(Boolean).pop();
        title.textContent = base || entry.id || "session";
        const meta = document.createElement("span");
        meta.className = "pi-session-meta";
        meta.textContent = [formatPiSessionTime(entry), cwd].filter(Boolean).join(" · ");
        button.append(title, meta);
        button.title = entry.path || "";
        button.addEventListener("click", () => finish(entry));
        list.appendChild(button);
      }
    }

    function load() {
      list.textContent = "Loading…";
      request({ type: "list_pi_sessions" })
        .then((result) => {
          if (settled) return;
          entries = Array.isArray(result.sessions) ? result.sessions : [];
          renderList();
        })
        .catch((error) => {
          if (settled) return;
          list.replaceChildren();
          const failure = document.createElement("div");
          failure.className = "pi-trust-empty";
          failure.textContent =
            error && error.message ? error.message : "Cannot list pi sessions";
          list.appendChild(failure);
        });
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(null);
      }
    }
    cancel.addEventListener("click", () => finish(null));
    overlay.addEventListener("pointerdown", (event) => {
      if (event.target === overlay) finish(null);
    });
    document.addEventListener("keydown", onKey, true);
    load();
    cancel.focus();
  }

  function showPiTrustDialog(cwd, onChoose, options) {
    const overlay = document.createElement("div");
    overlay.className = "pi-trust-overlay";
    const dialog = document.createElement("div");
    dialog.className = "pi-trust-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "Start pi session");

    const opts = options || {};
    const heading = document.createElement("h2");
    heading.textContent = opts.resume ? "Resume pi?" : "Start pi?";
    const copy = document.createElement("p");
    copy.textContent = opts.resume
      ? "This saved conversation will run with your user privileges. Choose whether " +
        "pi should load this project's settings and extensions."
      : "Project settings and extensions in this folder run with your user privileges. " +
        "Choose whether pi should load them.";

    const pathRow = document.createElement("div");
    pathRow.className = "pi-trust-path-row";
    const path = document.createElement("input");
    path.type = "text";
    path.className = "pi-trust-path";
    path.value = cwd || "";
    path.placeholder = "Server directory";
    path.setAttribute("aria-label", "Session directory");
    const go = document.createElement("button");
    go.type = "button";
    go.textContent = "Go";
    const up = document.createElement("button");
    up.type = "button";
    up.textContent = "Up";
    up.disabled = true;
    pathRow.append(path, go, up);

    const browser = document.createElement("div");
    browser.className = "pi-trust-browser";

    const actions = document.createElement("div");
    actions.className = "pi-trust-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    const deny = document.createElement("button");
    deny.type = "button";
    deny.textContent = "Start without project settings";
    const allow = document.createElement("button");
    allow.type = "button";
    allow.className = "trust";
    allow.textContent = "Start with project settings";
    actions.append(cancel, deny, allow);
    dialog.append(heading, copy, pathRow, browser, actions);
    if (opts.resume) {
      path.disabled = true;
      go.hidden = true;
      up.hidden = true;
      browser.hidden = true;
    }
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    let settled = false;
    let choosing = false;
    let current = { path: cwd || "", parent: null };
    let listSeq = 0;

    function directoryButton(label, target) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pi-trust-dir";
      button.textContent = label;
      button.title = target;
      button.addEventListener("click", () => navigate(target));
      return button;
    }

    function renderDirs(result) {
      browser.replaceChildren();
      if (result.parent) browser.appendChild(directoryButton("../", result.parent));
      for (const dir of result.dirs || []) {
        browser.appendChild(directoryButton(dir.name + "/", dir.path));
      }
      if (result.truncated) {
        const more = document.createElement("div");
        more.className = "pi-trust-empty";
        more.textContent = "More entries not shown";
        browser.appendChild(more);
      }
      if (!browser.children.length) {
        const empty = document.createElement("div");
        empty.className = "pi-trust-empty";
        empty.textContent = "No subdirectories";
        browser.appendChild(empty);
      }
    }

    async function navigate(target) {
      const seq = ++listSeq;
      browser.textContent = "Loading…";
      try {
        const result = await request({ type: "list_dirs", path: String(target || "") });
        if (settled || seq !== listSeq) return;
        current = result;
        path.value = result.path;
        up.disabled = !result.parent;
        renderDirs(result);
      } catch (error) {
        if (settled || seq !== listSeq) return;
        browser.replaceChildren();
        const failure = document.createElement("div");
        failure.className = "pi-trust-empty";
        failure.textContent =
          error && error.message ? error.message : "Cannot open directory";
        browser.appendChild(failure);
        up.disabled = true;
      }
    }

    function finish(value) {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      onChoose(value, path.value.trim());
    }

    async function choose(value) {
      if (settled || choosing) return;
      const target = path.value.trim();
      if (!target || target === current.path) {
        finish(value);
        return;
      }
      choosing = true;
      try {
        const result = await request({ type: "list_dirs", path: target });
        if (settled) return;
        path.value = result.path;
        finish(value);
      } catch (error) {
        if (settled) return;
        browser.replaceChildren();
        const failure = document.createElement("div");
        failure.className = "pi-trust-empty";
        failure.textContent =
          error && error.message ? error.message : "Cannot open directory";
        browser.appendChild(failure);
      } finally {
        choosing = false;
      }
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(null);
      }
    }
    path.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        navigate(path.value);
      }
    });
    go.addEventListener("click", () => navigate(path.value));
    up.addEventListener("click", () => {
      if (current.parent) navigate(current.parent);
    });
    cancel.addEventListener("click", () => finish(null));
    deny.addEventListener("click", () => choose("deny"));
    allow.addEventListener("click", () => choose("allow"));
    overlay.addEventListener("pointerdown", (event) => {
      if (event.target === overlay) finish(null);
    });
    document.addEventListener("keydown", onKey, true);
    navigate(path.value);
    deny.focus();
  }

  function createPiSession() {
    const pi = CONFIG.pi;
    if (!pi || !pi.available) {
      window.alert("pi is not installed on the server.");
      return;
    }
    const cwd = pi.cwd || "";
    showPiTrustDialog(cwd, (trust, chosenCwd) => {
      if (!trust) return;
      activateNext = true;
      send({
        type: "create",
        mode: "pi",
        cwd: chosenCwd || pi.cwd,
        title: "pi",
        trust,
      });
    });
  }

  function createPiResumeSession() {
    const pi = CONFIG.pi;
    if (!pi || !pi.available) {
      window.alert("pi is not installed on the server.");
      return;
    }
    showPiResumeDialog((entry) => {
      if (!entry) return;
      const cwd = entry.cwd || pi.cwd || "";
      showPiTrustDialog(
        cwd,
        (trust) => {
          if (!trust) return;
          activateNext = true;
          send({
            type: "create",
            mode: "pi",
            cwd,
            title: entry.id || "pi",
            trust,
            resume: entry.path,
          });
        },
        { resume: true }
      );
    });
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
      createSession();
    } else if (event.ctrlKey && event.shiftKey && (event.key === "B" || event.key === "b")) {
      event.preventDefault();
      setSidebarCollapsed(!isSidebarCollapsed());
    }
  });

  window.addEventListener("beforeunload", () => {
    closedByPage = true;
    if (socket) socket.close();
  });

  // ------------------------------------------------- native-app bridge

  // The desktop host (Tauri) drives a few UI actions from native menu items
  // via WebviewWindow.eval().  Expose only those actions; the page itself uses
  // no Tauri APIs, so the remote-origin webview needs no IPC surface.
  window.__webterm = {
    newSession: () => createSession(),
    toggleSidebar: () => setSidebarCollapsed(!isSidebarCollapsed()),
    closeActiveSession: () => {
      if (activeId) send({ type: "kill", session_id: activeId });
    },
  };

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
