/* Client-side transcript search for the structured agent views.
 *
 * The finder owns no transcript knowledge.  A view passes a provider for the
 * entries it can search (id, role, text) plus a reveal callback that scrolls to
 * one entry and opens its collapsed blocks, so both chat renderers share the
 * same bar without the finder depending on their DOM.
 *
 * Matching runs over the text the view rendered, so a match cannot point at
 * markup the reader cannot see.  The hit is marked in the bar's snippet rather
 * than inside the transcript: both renderers rebuild entry nodes while a reply
 * streams, so an in-place mark would vanish on the next delta.  The revealed
 * entry gets a transient outline instead.
 */
"use strict";

(function () {
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /** Start offsets of every non-overlapping occurrence of *needle*. */
  function occurrences(haystack, needle) {
    const found = [];
    if (!needle) return found;
    const text = String(haystack || "").toLowerCase();
    let from = 0;
    for (;;) {
      const at = text.indexOf(needle, from);
      if (at < 0) return found;
      found.push(at);
      from = at + needle.length;
    }
  }

  /** Render *text* around one occurrence, with the hit wrapped in a <mark>. */
  function renderSnippet(parent, text, at, length) {
    const radius = 48;
    const source = String(text || "");
    const start = Math.max(0, at - radius);
    const end = Math.min(source.length, at + length + radius);
    if (start > 0) parent.appendChild(el("span", "", "…"));
    parent.appendChild(el("span", "", source.slice(start, at)));
    parent.appendChild(el("mark", "", source.slice(at, at + length)));
    parent.appendChild(el("span", "", source.slice(at + length, end)));
    if (end < source.length) parent.appendChild(el("span", "", "…"));
  }

  function normalizeMatches(raw, query) {
    const matches = [];
    for (const entry of Array.isArray(raw) ? raw : []) {
      if (!entry || typeof entry.id !== "string" || !entry.id) continue;
      const text = typeof entry.text === "string" ? entry.text : "";
      for (const at of occurrences(text, query)) {
        matches.push({
          id: entry.id,
          role: entry.role || "message",
          text,
          at,
          length: query.length,
        });
      }
    }
    return matches;
  }

  /**
   * Mount a find bar into *host*.
   *
   * Options:
   *   items()        -> [{id, role, text}] in transcript order
   *   reveal(id)     -> scroll to the entry and open its collapsed blocks
   *   hasMore?()     -> whether earlier entries are not loaded yet
   *   loadEarlier?() -> promise; resolves false when nothing was loaded
   *   focusTarget?() -> element to focus when the bar closes
   *   onClose?()     -> extra teardown for the view
   */
  function create(host, options) {
    const settings = options || {};
    const bar = el("div", "find-bar");
    bar.hidden = true;

    const input = el("input", "find-input");
    input.type = "search";
    input.placeholder = "Search transcript… (Enter next, Shift+Enter previous, Esc closes)";
    input.setAttribute("aria-label", "Search transcript");
    const countEl = el("span", "find-count", "");
    const snippetEl = el("span", "find-snippet", "");
    const moreButton = el("button", "find-more", "search earlier messages");
    moreButton.type = "button";
    moreButton.hidden = true;
    const prevButton = el("button", "find-prev", "↑");
    prevButton.type = "button";
    prevButton.title = "Previous match";
    const nextButton = el("button", "find-next", "↓");
    nextButton.type = "button";
    nextButton.title = "Next match";
    const closeButton = el("button", "find-close", "✕");
    closeButton.type = "button";
    closeButton.title = "Close search";
    bar.append(input, countEl, snippetEl, moreButton, prevButton, nextButton, closeButton);
    host.append(bar);

    let query = "";
    let matches = [];
    let cursor = -1;
    let loading = false;

    function current() {
      return cursor >= 0 && cursor < matches.length ? matches[cursor] : null;
    }

    function revealCurrent() {
      const match = current();
      if (!match) return;
      if (typeof settings.reveal === "function") settings.reveal(match.id);
    }

    function renderBar() {
      prevButton.disabled = !matches.length || loading;
      nextButton.disabled = !matches.length || loading;
      if (!query) {
        countEl.textContent = "";
      } else if (loading) {
        countEl.textContent = "searching…";
      } else if (!matches.length) {
        countEl.textContent = "no matches";
      } else {
        countEl.textContent = cursor + 1 + " / " + matches.length;
      }
      const match = current();
      snippetEl.replaceChildren();
      if (match) {
        snippetEl.appendChild(el("span", "find-role", match.role));
        renderSnippet(snippetEl, match.text, match.at, match.length);
      }
      const exhausted = Boolean(settings.hasMore && settings.hasMore());
      moreButton.hidden = loading || !query || matches.length > 0 || !exhausted;
      moreButton.disabled = loading;
    }

    function search(value) {
      query = String(value || "").trim().toLowerCase();
      matches = query ? normalizeMatches(settings.items ? settings.items() : [], query) : [];
      cursor = matches.length ? 0 : -1;
      renderBar();
      revealCurrent();
    }

    function step(delta) {
      if (!matches.length) return;
      cursor = (cursor + delta + matches.length) % matches.length;
      renderBar();
      revealCurrent();
    }

    function loadMore() {
      if (loading || typeof settings.loadEarlier !== "function") return;
      loading = true;
      renderBar();
      Promise.resolve(settings.loadEarlier())
        .then((loaded) => {
          loading = false;
          if (loaded === false) renderBar();
          else search(query);
        })
        .catch(() => {
          loading = false;
          renderBar();
        });
    }

    function open() {
      bar.hidden = false;
      input.focus();
      if (query) search(input.value);
      else renderBar();
    }

    function close() {
      bar.hidden = true;
      if (typeof settings.onClose === "function") settings.onClose();
      const target = settings.focusTarget ? settings.focusTarget() : null;
      if (target && typeof target.focus === "function") target.focus();
    }

    function toggle() {
      if (bar.hidden) open();
      else close();
    }

    input.addEventListener("input", () => search(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        step(event.shiftKey ? -1 : 1);
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    });
    prevButton.addEventListener("click", () => step(-1));
    nextButton.addEventListener("click", () => step(1));
    closeButton.addEventListener("click", close);
    moreButton.addEventListener("click", loadMore);

    return {
      bar,
      open,
      close,
      toggle,
      isOpen: () => !bar.hidden,
      setQuery: (value) => {
        input.value = String(value || "");
        search(input.value);
      },
      matches: () => matches.slice(),
      step,
    };
  }

  window.WebtermFind = { create };
})();
