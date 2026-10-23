/* Literal `@path` completion for the chat composers.
 *
 * RPC-mode pi has no `@file` expansion, so a mention here is plain text: the
 * picker replaces the `@token` with the absolute path the server listed. The
 * token starts at the beginning of the message or after whitespace and runs to
 * the caret without spaces, so `@src/pi` asks for `src/` entries whose names
 * start with `pi`. A path with whitespace is inserted double-quoted (with `\`,
 * `"`, `$` and `` ` `` escaped) so the model and a shell read it as one word.
 *
 * The composer calls `keydown(event)` before its own Enter/Escape handling;
 * a consumed key (arrows, Tab, Enter, Escape) must not reach it, which is what
 * keeps Enter with the picker open from sending the prompt.
 */
(function () {
  "use strict";

  /** Suggestions rendered at once; the server caps the listing itself. */
  const MAX_ITEMS = 50;
  /** `@` at the start of the text or after whitespace, to the end of `before`. */
  const TOKEN = /(^|\s)@(\S*)$/;
  const WHITESPACE = /\s/;
  const CONTROL = /[\r\n]/;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /**
   * Return the `@` token ending at *caret*, or null when the caret is not in
   * one. A token must end at the caret for the suggestion to be unambiguous.
   */
  function tokenAt(value, caret) {
    const text = String(value === undefined || value === null ? "" : value);
    const at = Math.max(0, Math.min(Number(caret) || 0, text.length));
    const after = text.slice(at);
    if (after && !WHITESPACE.test(after[0])) return null;
    const before = text.slice(0, at);
    const match = TOKEN.exec(before);
    if (!match) return null;
    const query = match[2];
    return { start: at - query.length - 1, end: at, query: query };
  }

  /** Split a token body into its directory part (with slash) and name prefix. */
  function splitQuery(query) {
    const text = String(query || "");
    const slash = text.lastIndexOf("/");
    if (slash < 0) return { dir: "", prefix: text };
    return { dir: text.slice(0, slash + 1), prefix: text.slice(slash + 1) };
  }

  /** Resolve a token's directory against the composer's base directory. */
  function resolveDir(base, dir) {
    if (!dir) return base || "";
    if (dir[0] === "/" || dir[0] === "~") return dir;
    if (!base) return dir;
    return base.replace(/\/+$/, "") + "/" + dir;
  }

  /** Quote a path that contains whitespace; everything else stays literal. */
  function quotePath(path) {
    const text = String(path);
    if (!WHITESPACE.test(text)) return text;
    return '"' + text.replace(/[\\"$`]/g, "\\$&") + '"';
  }

  function create(options) {
    const input = options.input;
    const listEntries =
      typeof options.listEntries === "function" ? options.listEntries : null;
    const basePath = typeof options.basePath === "string" ? options.basePath : "";
    const maxItems = options.maxItems > 0 ? options.maxItems : MAX_ITEMS;

    const element = el("div", "path-complete");
    element.hidden = true;
    const state = { items: [], index: 0, token: null, seq: 0, active: null };

    function reset() {
      state.items = [];
      state.index = 0;
      state.active = null;
      element.hidden = true;
      element.replaceChildren();
    }

    function close() {
      state.token = null;
      // Bump the sequence so an in-flight listing cannot reopen the picker.
      state.seq += 1;
      reset();
    }

    function isOpen() {
      return !element.hidden && state.items.length > 0;
    }

    function scrollToActive() {
      const node = state.active;
      if (!node || typeof node.scrollIntoView !== "function") return;
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => node.scrollIntoView({ block: "nearest" }));
      } else {
        node.scrollIntoView({ block: "nearest" });
      }
    }

    function render() {
      element.replaceChildren();
      state.active = null;
      if (!state.items.length) {
        element.hidden = true;
        return;
      }
      element.hidden = false;
      state.items.forEach((entry, index) => {
        const isDir = entry.type === "dir";
        const item = el(
          "div",
          "path-complete-item" + (index === state.index ? " active" : "")
        );
        item.appendChild(
          el("span", "path-complete-name", isDir ? entry.name + "/" : entry.name)
        );
        item.appendChild(el("span", "path-complete-kind", isDir ? "dir" : "file"));
        item.title = String(entry.path || entry.name);
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          accept(index);
        });
        if (index === state.index) state.active = item;
        element.appendChild(item);
      });
      scrollToActive();
    }

    function replaceToken(token, text) {
      const value = String(input.value === undefined ? "" : input.value);
      input.value = value.slice(0, token.start) + text + value.slice(token.end);
      const caret = token.start + text.length;
      if (typeof input.setSelectionRange === "function") {
        input.setSelectionRange(caret, caret);
      }
      state.token = null;
    }

    function accept(index) {
      const token = state.token;
      const entry = state.items[index];
      if (!token || !entry) return;
      const isDir = entry.type === "dir";
      const path = String(entry.path || entry.name);
      if (isDir && !WHITESPACE.test(path)) {
        // Keep the `@` so the picker descends into the chosen directory.
        replaceToken(token, "@" + path.replace(/\/+$/, "") + "/");
        update();
        return;
      }
      const literal = isDir ? path.replace(/\/+$/, "") + "/" : path;
      replaceToken(token, quotePath(literal));
      close();
    }

    function move(delta) {
      if (!state.items.length) return;
      state.index = (state.index + delta + state.items.length) % state.items.length;
      render();
    }

    function update() {
      if (!listEntries || input.disabled) {
        close();
        return;
      }
      const value = String(input.value === undefined ? "" : input.value);
      const caret =
        typeof input.selectionStart === "number" ? input.selectionStart : value.length;
      const token = tokenAt(value, caret);
      if (!token) {
        close();
        return;
      }
      state.token = token;
      const seq = ++state.seq;
      const parts = splitQuery(token.query);
      const dir = resolveDir(basePath, parts.dir);
      Promise.resolve(listEntries(dir))
        .then((result) => {
          if (seq !== state.seq || !state.token) return;
          const entries = result && Array.isArray(result.entries) ? result.entries : [];
          const needle = parts.prefix.toLowerCase();
          const items = [];
          for (const entry of entries) {
            if (!entry || typeof entry.name !== "string") continue;
            // A newline would split the inserted mention across lines.
            if (CONTROL.test(entry.name)) continue;
            if (!entry.name.toLowerCase().startsWith(needle)) continue;
            items.push(entry);
            if (items.length >= maxItems) break;
          }
          state.items = items;
          if (state.index >= items.length) state.index = 0;
          render();
        })
        .catch(() => {
          if (seq === state.seq) close();
        });
    }

    /**
     * Handle a composer keydown. Returns true when the picker consumed it, so
     * the composer must not send, abort, or move anything else.
     */
    function keydown(event) {
      if (!isOpen()) return false;
      const key = event.key;
      if (key === "ArrowDown" || key === "ArrowUp") {
        event.preventDefault();
        move(key === "ArrowDown" ? 1 : -1);
        return true;
      }
      if (key === "Tab") {
        event.preventDefault();
        accept(state.index);
        return true;
      }
      if (key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
        event.preventDefault();
        accept(state.index);
        return true;
      }
      if (key === "Escape") {
        event.preventDefault();
        close();
        return true;
      }
      return false;
    }

    function onInput() {
      update();
    }

    function onBlur() {
      close();
    }

    if (typeof input.addEventListener === "function") {
      input.addEventListener("input", onInput);
      input.addEventListener("click", onInput);
      input.addEventListener("blur", onBlur);
    }

    return {
      element: element,
      update: update,
      close: close,
      keydown: keydown,
      isOpen: isOpen,
      destroy: () => {
        if (typeof input.removeEventListener === "function") {
          input.removeEventListener("input", onInput);
          input.removeEventListener("click", onInput);
          input.removeEventListener("blur", onBlur);
        }
        close();
        element.remove();
      },
    };
  }

  window.WebtermPathComplete = {
    create: create,
    tokenAt: tokenAt,
    splitQuery: splitQuery,
    resolveDir: resolveDir,
    quotePath: quotePath,
  };
})();
