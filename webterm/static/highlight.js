/* Safe, dependency-free highlighting for pi chat text.
 *
 * Everything is built from DOM text nodes. Model and tool output never touch
 * innerHTML. Unknown languages fall back to plain preformatted text, and
 * unified diffs get line-level colors.
 */
"use strict";

(function () {
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const KEYWORDS = {
    python: [
      "and", "as", "assert", "async", "await", "break", "class", "continue", "def",
      "del", "elif", "else", "except", "finally", "for", "from", "global", "if",
      "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
      "return", "try", "while", "with", "yield", "match", "case",
    ],
    javascript: [
      "async", "await", "break", "case", "catch", "class", "const", "continue",
      "debugger", "default", "delete", "do", "else", "export", "extends", "finally",
      "for", "from", "function", "get", "if", "import", "in", "instanceof", "let",
      "new", "of", "return", "set", "static", "super", "switch", "this", "throw",
      "try", "typeof", "var", "void", "while", "with", "yield",
    ],
    typescript: [
      "abstract", "any", "as", "asserts", "async", "await", "boolean", "break",
      "case", "catch", "class", "const", "constructor", "continue", "declare",
      "default", "delete", "do", "else", "enum", "export", "extends", "false",
      "finally", "for", "from", "function", "get", "if", "implements", "import",
      "in", "infer", "instanceof", "interface", "is", "keyof", "let", "namespace",
      "never", "new", "null", "number", "of", "private", "protected", "public",
      "readonly", "return", "satisfies", "set", "static", "string", "super",
      "switch", "symbol", "this", "throw", "true", "try", "type", "typeof",
      "undefined", "unknown", "var", "void", "while", "yield",
    ],
    rust: [
      "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else",
      "enum", "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop",
      "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
      "static", "struct", "super", "trait", "true", "type", "unsafe", "use",
      "where", "while", "yield",
    ],
    go: [
      "break", "case", "chan", "const", "continue", "default", "defer", "else",
      "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
      "map", "package", "range", "return", "select", "struct", "switch", "type",
      "var",
    ],
    shell: [
      "case", "do", "done", "elif", "else", "esac", "fi", "for", "function", "if",
      "in", "select", "then", "time", "until", "while",
    ],
    yaml: ["true", "false", "null", "yes", "no", "on", "off"],
    toml: ["true", "false"],
    json: [],
  };

  const LITERALS = {
    python: ["True", "False", "None"],
    javascript: ["true", "false", "null", "undefined", "NaN", "Infinity"],
    typescript: ["true", "false", "null", "undefined"],
    rust: ["true", "false", "None", "Some", "Ok", "Err"],
    go: ["true", "false", "nil", "iota"],
    shell: ["true", "false"],
    json: ["true", "false", "null"],
    yaml: ["true", "false", "null"],
    toml: ["true", "false"],
  };

  const SPECS = {
    python: { line: "#", quotes: "'\"", triple: true, keywords: KEYWORDS.python, literals: LITERALS.python },
    javascript: { line: "//", block: ["/*", "*/"], quotes: "'\"`", keywords: KEYWORDS.javascript, literals: LITERALS.javascript },
    typescript: { line: "//", block: ["/*", "*/"], quotes: "'\"`", keywords: KEYWORDS.typescript, literals: LITERALS.typescript },
    rust: { line: "//", block: ["/*", "*/"], quotes: "\"`", keywords: KEYWORDS.rust, literals: LITERALS.rust },
    go: { line: "//", block: ["/*", "*/"], quotes: "\"`", keywords: KEYWORDS.go, literals: LITERALS.go },
    shell: { line: "#", quotes: "'\"", keywords: KEYWORDS.shell, literals: LITERALS.shell, variables: true },
    json: { quotes: "\"", keywords: KEYWORDS.json, literals: LITERALS.json },
    yaml: { line: "#", quotes: "'\"", keywords: KEYWORDS.yaml, literals: LITERALS.yaml },
    toml: { line: "#", quotes: "'\"", keywords: KEYWORDS.toml, literals: LITERALS.toml },
    css: { block: ["/*", "*/"], quotes: "'\"" },
  };

  const ALIASES = {
    py: "python",
    js: "javascript",
    jsx: "javascript",
    mjs: "javascript",
    cjs: "javascript",
    ts: "typescript",
    tsx: "typescript",
    sh: "shell",
    bash: "shell",
    zsh: "shell",
    console: "shell",
    yml: "yaml",
    rs: "rust",
    golang: "go",
    jsonc: "json",
  };

  const tokenCache = new Map();
  const TOKEN_CACHE_LIMIT = 400;

  function normaliseLanguage(raw) {
    const lang = String(raw || "").trim().toLowerCase().split(/\s+/)[0];
    return ALIASES[lang] || lang;
  }

  function isIdentStart(ch) {
    return /[A-Za-z_]/.test(ch);
  }

  function isIdentPart(ch) {
    return /[A-Za-z0-9_]/.test(ch);
  }

  function tokenize(code, lang) {
    const spec = SPECS[lang];
    if (!spec) return null;
    const keywords = new Set(spec.keywords || []);
    const literals = new Set(spec.literals || []);
    const tokens = [];
    let i = 0;
    const n = code.length;

    function push(cls, end) {
      if (end > i) tokens.push({ text: code.slice(i, end), cls });
      i = end;
    }

    while (i < n) {
      const ch = code[i];
      const next = code[i + 1];

      if (spec.line && code.startsWith(spec.line, i)) {
        let end = code.indexOf("\n", i);
        if (end < 0) end = n;
        push("comment", end);
        continue;
      }

      if (spec.block && code.startsWith(spec.block[0], i)) {
        let end = code.indexOf(spec.block[1], i + spec.block[0].length);
        end = end < 0 ? n : end + spec.block[1].length;
        push("comment", end);
        continue;
      }

      if (spec.triple && (code.startsWith('"""', i) || code.startsWith("'''", i))) {
        const quote = code.slice(i, i + 3);
        let end = code.indexOf(quote, i + 3);
        end = end < 0 ? n : end + 3;
        push("string", end);
        continue;
      }

      if (spec.quotes && spec.quotes.includes(ch)) {
        const quote = ch;
        let j = i + 1;
        while (j < n) {
          if (code[j] === "\\") {
            j += 2;
            continue;
          }
          if (code[j] === quote) {
            j += 1;
            break;
          }
          if (code[j] === "\n" && quote !== "`") {
            break;
          }
          j += 1;
        }
        push("string", Math.min(j, n));
        continue;
      }

      if (spec.variables && ch === "$") {
        const match = /^\$(?:\{[^}\n]*\}|[A-Za-z_][A-Za-z0-9_]*|[0-9@*?#$!-])/.exec(
          code.slice(i)
        );
        if (match) {
          push("variable", i + match[0].length);
          continue;
        }
      }

      if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(next || ""))) {
        const match = /^(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(
          code.slice(i)
        );
        if (match) {
          push("number", i + match[0].length);
          continue;
        }
      }

      if (isIdentStart(ch)) {
        let j = i + 1;
        while (j < n && isIdentPart(code[j])) j += 1;
        const word = code.slice(i, j);
        let cls = "";
        if (keywords.has(word)) cls = "keyword";
        else if (literals.has(word)) cls = "literal";
        else {
          let k = j;
          while (k < n && /\s/.test(code[k])) k += 1;
          if (code[k] === "(") cls = "function";
        }
        if (cls) push(cls, j);
        else {
          tokens.push({ text: code.slice(i, j), cls: "" });
          i = j;
        }
        continue;
      }

      tokens.push({ text: ch, cls: "" });
      i += 1;
    }
    return tokens;
  }

  function cachedTokens(code, lang) {
    const key = lang + "\u0000" + code;
    if (tokenCache.has(key)) return tokenCache.get(key);
    const tokens = tokenize(code, lang);
    if (tokenCache.size >= TOKEN_CACHE_LIMIT) {
      tokenCache.delete(tokenCache.keys().next().value);
    }
    tokenCache.set(key, tokens);
    return tokens;
  }

  function appendTokens(pre, code, lang) {
    const tokens = cachedTokens(code, lang);
    if (!tokens) {
      pre.textContent = code;
      return;
    }
    for (const token of tokens) {
      if (token.cls) pre.appendChild(el("span", "tok-" + token.cls, token.text));
      else pre.appendChild(document.createTextNode(token.text));
    }
  }

  function looksLikeDiff(text) {
    const lines = String(text || "").split("\n");
    let hunk = false;
    let marker = false;
    for (const line of lines) {
      if (/^@@ .*@@/.test(line)) hunk = true;
      if (/^(diff --git |index |--- |\+\+\+ )/.test(line)) marker = true;
    }
    return hunk || (marker && lines.some((line) => /^[+-]/.test(line)));
  }

  function diffLineClass(line) {
    if (/^(diff --git |index |new file mode|deleted file mode|--- |\+\+\+ )/.test(line)) {
      return "pi-diff-meta";
    }
    if (/^@@/.test(line)) return "pi-diff-hunk";
    if (/^\+/.test(line)) return "pi-diff-add";
    if (/^-/.test(line)) return "pi-diff-del";
    return "pi-diff-context";
  }

  function appendDiff(pre, text) {
    const lines = String(text || "").split("\n");
    lines.forEach((line, index) => {
      pre.appendChild(el("span", "pi-diff-line " + diffLineClass(line), line));
      if (index < lines.length - 1) pre.appendChild(document.createTextNode("\n"));
    });
  }

  function codeBlock(parent, code, lang, options) {
    const opts = options || {};
    const wrap = el("div", "pi-code-block");
    if (opts.header !== false) {
      const head = el("div", "pi-code-head");
      head.appendChild(el("span", "pi-code-lang", lang || "text"));
      wrap.appendChild(head);
    }
    const pre = el("pre", "pi-pre pi-code" + (lang === "diff" ? " pi-diff" : ""));
    if (lang === "diff") appendDiff(pre, code);
    else appendTokens(pre, code, lang);
    wrap.appendChild(pre);
    parent.appendChild(wrap);
    return wrap;
  }


  const ESCAPABLE = /[\\`*_{}\[\]()#+\-.!|~>]/;

  function safeLink(raw) {
    try {
      const parsed = new URL(String(raw || ""), window.location.href);
      if (!["http:", "https:", "mailto:"].includes(parsed.protocol)) return null;
      return parsed.href;
    } catch {
      return null;
    }
  }

  function appendLink(parent, label, rawUrl, title, plainLabel, prefix) {
    const href = safeLink(rawUrl);
    if (!href) {
      parent.appendChild(document.createTextNode((prefix || "") + `[${label}](${rawUrl})`));
      return;
    }
    const link = el("a", "pi-md-link");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (title) link.title = String(title).replace(/^["']|[\"']$/g, "");
    if (plainLabel) link.textContent = label;
    else renderInline(link, label, { links: false });
    parent.appendChild(link);
  }

  function isWordChar(ch) {
    return Boolean(ch) && /[A-Za-z0-9]/.test(ch);
  }

  function unescapeMd(text) {
    return String(text || "").replace(/\\([\\`*_{}\[\]()#+\-.!|~>])/g, "$1");
  }

  function parseCodeSpan(source, i) {
    let run = 1;
    while (source[i + run] === "`") run += 1;
    const marker = source.slice(i, i + run);
    let end = source.indexOf(marker, i + run);
    while (end !== -1) {
      if (source[end - 1] !== "`" && source[end + run] !== "`") break;
      end = source.indexOf(marker, end + run);
    }
    if (end === -1) return null;
    let content = source.slice(i + run, end);
    if (content.length >= 2 && content.startsWith(" ") && content.endsWith(" ") && content.trim()) {
      content = content.slice(1, -1);
    }
    return { content: content.replace(/\s*\n\s*/g, " "), end: end + run };
  }

  function findClosingBracket(source, start) {
    let depth = 0;
    for (let i = start; i < source.length; i += 1) {
      const ch = source[i];
      if (ch === "\\") {
        i += 1;
        continue;
      }
      if (ch === "[") depth += 1;
      else if (ch === "]") {
        depth -= 1;
        if (depth === 0) return i;
      }
    }
    return -1;
  }

  function findClosingParen(source, start) {
    let depth = 0;
    for (let i = start; i < source.length; i += 1) {
      const ch = source[i];
      if (ch === "\\") {
        i += 1;
        continue;
      }
      if (ch === "\"" || ch === "'") {
        const quote = ch;
        for (i += 1; i < source.length; i += 1) {
          if (source[i] === "\\") i += 1;
          else if (source[i] === quote) break;
        }
        continue;
      }
      if (ch === "<") {
        const end = source.indexOf(">", i + 1);
        if (end !== -1) i = end;
        continue;
      }
      if (ch === "(") depth += 1;
      else if (ch === ")") {
        depth -= 1;
        if (depth === 0) return i;
      }
    }
    return -1;
  }

  function parseLinkTarget(inner) {
    let target = String(inner || "").trim();
    if (!target) return null;
    if (target.startsWith("<")) {
      const end = target.indexOf(">");
      if (end < 0) return null;
      return {
        url: unescapeMd(target.slice(1, end)),
        title: target.slice(end + 1).trim(),
      };
    }
    let depth = 0;
    let end = target.length;
    for (let i = 0; i < target.length; i += 1) {
      const ch = target[i];
      if (ch === "\\") {
        i += 1;
        continue;
      }
      if (ch === "(") depth += 1;
      else if (ch === ")") depth = Math.max(0, depth - 1);
      else if (/\s/.test(ch) && depth === 0) {
        end = i;
        break;
      }
    }
    return {
      url: unescapeMd(target.slice(0, end).trim()),
      title: target.slice(end).trim(),
    };
  }

  function parseLinkAt(source, i) {
    const image = source[i] === "!" && source[i + 1] === "[";
    const open = image ? i + 1 : i;
    if (source[open] !== "[") return null;
    const close = findClosingBracket(source, open);
    if (close < 0 || source[close + 1] !== "(") return null;
    const end = findClosingParen(source, close + 1);
    if (end < 0) return null;
    const target = parseLinkTarget(source.slice(close + 2, end));
    if (!target || !target.url) return null;
    return {
      image,
      label: source.slice(open + 1, close),
      url: target.url,
      title: target.title,
      end: end + 1,
    };
  }

  function parseAutolink(source, i) {
    const end = source.indexOf(">", i + 1);
    if (end < 0) return null;
    const inner = source.slice(i + 1, end).trim();
    if (!inner) return null;
    if (/^(?:https?:\/\/|mailto:)[^\s<>]+$/i.test(inner)) {
      return { label: inner, url: inner, end: end + 1 };
    }
    if (/^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$/.test(inner)) {
      return { label: inner, url: "mailto:" + inner, end: end + 1 };
    }
    return null;
  }

  function trimUrlEnd(url) {
    let end = url.length;
    while (end > 0 && /[.,;:!?'"]/.test(url[end - 1])) end -= 1;
    let opens = 0;
    let closes = 0;
    for (let i = 0; i < end; i += 1) {
      if (url[i] === "(") opens += 1;
      else if (url[i] === ")") closes += 1;
    }
    while (end > 0 && url[end - 1] === ")" && closes > opens) {
      end -= 1;
      closes -= 1;
    }
    return url.slice(0, end);
  }

  function matchBareUrl(source, i) {
    if (i > 0 && /[A-Za-z0-9@]/.test(source[i - 1])) return null;
    const match = /^(?:https?:\/\/|www\.)[^\s<>"'`]+/i.exec(source.slice(i));
    if (!match) return null;
    const url = trimUrlEnd(match[0]);
    const minimum = /^https?:\/\//i.test(url) ? 8 : 4;
    if (url.length < minimum || !/[A-Za-z0-9]/.test(url)) return null;
    return { url, end: i + url.length };
  }

  function findDelimiter(source, start, marker) {
    let found = start;
    while ((found = source.indexOf(marker, found)) !== -1) {
      if (found > 0 && source[found - 1] === "\\") {
        found += marker.length;
        continue;
      }
      if (marker[0] === "_" && isWordChar(source[found + marker.length])) {
        found += marker.length;
        continue;
      }
      if (marker.length === 1 && source[found + 1] === marker) {
        found += 1;
        continue;
      }
      if (!/\s/.test(source[found - 1] || "")) return found;
      found += marker.length;
    }
    return -1;
  }

  function renderInline(parent, text, options) {
    const opts = options || {};
    const links = opts.links !== false;
    const source = String(text || "");
    let buffer = "";

    function flush() {
      if (!buffer) return;
      parent.appendChild(document.createTextNode(buffer));
      buffer = "";
    }

    let i = 0;
    while (i < source.length) {
      const ch = source[i];
      const next = source[i + 1];

      if (ch === "\\" && next && ESCAPABLE.test(next)) {
        buffer += next;
        i += 2;
        continue;
      }

      if (ch === "`") {
        const span = parseCodeSpan(source, i);
        if (span) {
          flush();
          parent.appendChild(el("code", "pi-inline-code", span.content));
          i = span.end;
          continue;
        }
      }

      if (links && ((ch === "!" && next === "[") || ch === "[")) {
        const link = parseLinkAt(source, i);
        if (link) {
          flush();
          if (link.image) {
            appendLink(parent, link.label || "image", link.url, link.title, true, "!");
          } else appendLink(parent, link.label, link.url, link.title, false);
          i = link.end;
          continue;
        }
      }

      if (ch === "<") {
        const auto = parseAutolink(source, i);
        if (auto) {
          flush();
          appendLink(parent, auto.label, auto.url, "", true);
          i = auto.end;
          continue;
        }
      }

      if (links && /[hHwW]/.test(ch)) {
        const auto = matchBareUrl(source, i);
        if (auto) {
          flush();
          const url = /^www\./i.test(auto.url) ? "https://" + auto.url : auto.url;
          appendLink(parent, auto.url, url, "", true);
          i = auto.end;
          continue;
        }
      }

      if (ch === "~" && next === "~") {
        const close = findDelimiter(source, i + 2, "~~");
        if (close > i + 2) {
          flush();
          const deleted = el("del");
          renderInline(deleted, source.slice(i + 2, close), opts);
          parent.appendChild(deleted);
          i = close + 2;
          continue;
        }
      }

      if (ch === "*" || ch === "_") {
        let run = 1;
        while (source[i + run] === ch) run += 1;
        if (ch === "_" && isWordChar(source[i - 1])) {
          buffer += ch;
          i += 1;
          continue;
        }
        const lengths = run >= 3 ? [3, 2, 1] : run >= 2 ? [2, 1] : [1];
        let matched = false;
        for (const length of lengths) {
          if (length > run) continue;
          const marker = ch.repeat(length);
          if (/\s/.test(source[i + length] || "")) continue;
          const close = findDelimiter(source, i + length, marker);
          if (close <= i + length) continue;
          flush();
          const content = source.slice(i + length, close);
          if (length === 3) {
            const strong = el("strong");
            const emph = el("em");
            renderInline(emph, content, opts);
            strong.appendChild(emph);
            parent.appendChild(strong);
          } else if (length === 2) {
            const strong = el("strong");
            renderInline(strong, content, opts);
            parent.appendChild(strong);
          } else {
            const emph = el("em");
            renderInline(emph, content, opts);
            parent.appendChild(emph);
          }
          i = close + length;
          matched = true;
          break;
        }
        if (matched) continue;
      }

      buffer += ch;
      i += 1;
    }
    flush();
  }

  function fence(lines, i) {
    const match = /^ {0,3}(`{3,}|~{3,})([^\n]*)$/.exec(lines[i]);
    if (!match) return null;
    const marker = match[1][0];
    const length = match[1].length;
    const info = match[2].trim();
    if (marker === "`" && info.includes("`")) return null;
    const indent = /^ */.exec(lines[i])[0].length;
    const lang = normaliseLanguage(info);
    const code = [];
    const closing = new RegExp("^ {0,3}" + marker + "{" + length + ",}[ \\t]*$");
    let next = i + 1;
    while (next < lines.length && !closing.test(lines[next])) {
      code.push(lines[next].replace(new RegExp("^ {0," + indent + "}"), ""));
      next += 1;
    }
    if (next < lines.length) next += 1;
    return { code: code.join("\n"), lang, next };
  }

  function splitTableRow(line) {
    let row = String(line || "").trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
    const cells = [];
    let cell = "";
    let code = false;
    for (let i = 0; i < row.length; i += 1) {
      const ch = row[i];
      if (ch === "\\" && i + 1 < row.length) {
        cell += ch + row[i + 1];
        i += 1;
        continue;
      }
      if (ch === "`") {
        code = !code;
        cell += ch;
        continue;
      }
      if (ch === "|" && !code) {
        cells.push(cell.trim());
        cell = "";
        continue;
      }
      cell += ch;
    }
    cells.push(cell.trim());
    return cells;
  }

  function tableSeparator(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function tableStart(lines, i) {
    if (i + 1 >= lines.length || !lines[i].includes("|")) return false;
    if (!tableSeparator(lines[i + 1])) return false;
    return splitTableRow(lines[i]).length === splitTableRow(lines[i + 1]).length;
  }

  function renderTable(parent, lines, i) {
    const header = splitTableRow(lines[i]);
    const align = splitTableRow(lines[i + 1]).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      return "";
    });
    const table = el("table", "pi-md-table");
    const thead = el("thead");
    const headRow = el("tr");
    header.forEach((cell, index) => {
      const th = el("th", "pi-md-cell");
      if (align[index]) th.style.textAlign = align[index];
      renderInline(th, cell);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    let next = i + 2;
    while (next < lines.length && lines[next].trim() && lines[next].includes("|")) {
      const cells = splitTableRow(lines[next]);
      const row = el("tr");
      header.forEach((cell, index) => {
        const td = el("td", "pi-md-cell");
        if (align[index]) td.style.textAlign = align[index];
        renderInline(td, cells[index] || "");
        row.appendChild(td);
      });
      tbody.appendChild(row);
      next += 1;
    }
    table.appendChild(tbody);
    const wrap = el("div", "pi-md-table-wrap");
    wrap.appendChild(table);
    parent.appendChild(wrap);
    return next;
  }

  function leadingIndent(line) {
    const match = /^[ \t]*/.exec(line);
    return match ? match[0].replace(/\t/g, "    ").length : 0;
  }

  function listItem(line) {
    const match = /^([ \t]*)([-+*]|\d{1,9}[.)])([ \t]+|$)(.*)$/.exec(line);
    if (!match) return null;
    return {
      indent: match[1].replace(/\t/g, "    ").length,
      ordered: /^\d/.test(match[2]),
      start: parseInt(match[2], 10),
      content: match[4] || "",
    };
  }

  function renderList(parent, lines, i) {
    const first = listItem(lines[i]);
    if (!first) return i;
    const ordered = first.ordered;
    const baseIndent = first.indent;
    const list = el(ordered ? "ol" : "ul", "pi-md-list");
    if (ordered && first.start && first.start !== 1) list.start = String(first.start);
    parent.appendChild(list);
    let current = null;
    let next = i;

    while (next < lines.length) {
      const line = lines[next];
      if (!line.trim()) {
        let look = next + 1;
        while (look < lines.length && !lines[look].trim()) look += 1;
        const upcoming = look < lines.length ? listItem(lines[look]) : null;
        if (
          upcoming &&
          (upcoming.indent > baseIndent ||
            (upcoming.indent === baseIndent && upcoming.ordered === ordered))
        ) {
          next = look;
          continue;
        }
        break;
      }
      const item = listItem(line);
      if (!item) {
        if (!current) break;
        if (leadingIndent(line) <= baseIndent) break;
        current.appendChild(document.createTextNode("\n"));
        renderInline(current, line.trim());
        next += 1;
        continue;
      }
      if (item.indent < baseIndent || item.ordered !== ordered) break;
      if (item.indent > baseIndent) {
        if (!current) break;
        next = renderList(current, lines, next);
        continue;
      }

      current = el("li", "pi-md-li");
      const task = /^\[([ xX])\]\s+(.*)$/.exec(item.content);
      if (task) {
        current.classList.add("pi-md-li-task");
        const box = el("span", "pi-md-task", task[1].toLowerCase() === "x" ? "☑" : "☐");
        box.setAttribute("aria-hidden", "true");
        current.appendChild(box);
        renderInline(current, task[2]);
      } else {
        renderInline(current, item.content);
      }
      list.appendChild(current);
      next += 1;

      while (next < lines.length) {
        if (!lines[next].trim()) break;
        if (listItem(lines[next])) break;
        if (leadingIndent(lines[next]) <= baseIndent) break;
        current.appendChild(document.createTextNode("\n"));
        renderInline(current, lines[next].trim());
        next += 1;
      }
    }
    return next;
  }

  function indentedCode(lines, i) {
    if (!/^( {4}|\t)/.test(lines[i])) return null;
    const code = [];
    let next = i;
    while (next < lines.length) {
      if (!lines[next].trim()) {
        code.push("");
        next += 1;
        continue;
      }
      if (!/^( {4}|\t)/.test(lines[next])) break;
      code.push(lines[next].replace(/^( {4}|\t)/, ""));
      next += 1;
    }
    while (code.length && !code[code.length - 1]) code.pop();
    return { code: code.join("\n"), next };
  }

  function renderMarkdown(parent, text) {
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    let paragraph = [];
    let i = 0;

    function flushParagraph() {
      if (!paragraph.length) return;
      const content = paragraph.join("\n").replace(/[ \t]+$/gm, "");
      paragraph = [];
      if (!content.trim()) return;
      const node = el("div", "pi-md-p");
      renderInline(node, content);
      parent.appendChild(node);
    }

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        flushParagraph();
        i += 1;
        continue;
      }

      const block = fence(lines, i);
      if (block) {
        flushParagraph();
        codeBlock(parent, block.code, block.lang, { header: true });
        i = block.next;
        continue;
      }

      const heading = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+[ \t]*)?$/.exec(line);
      if (heading) {
        flushParagraph();
        const level = heading[1].length;
        const node = el("h" + level, "pi-md-heading");
        renderInline(node, heading[2] || "");
        parent.appendChild(node);
        i += 1;
        continue;
      }

      if (/^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/.test(line)) {
        flushParagraph();
        parent.appendChild(el("hr", "pi-md-hr"));
        i += 1;
        continue;
      }

      if (/^ {0,3}>/.test(line)) {
        flushParagraph();
        const quote = [];
        while (i < lines.length && /^ {0,3}>/.test(lines[i])) {
          quote.push(lines[i].replace(/^ {0,3}> ?/, ""));
          i += 1;
        }
        const node = el("blockquote", "pi-md-blockquote");
        renderMarkdown(node, quote.join("\n"));
        parent.appendChild(node);
        continue;
      }

      if (tableStart(lines, i)) {
        flushParagraph();
        i = renderTable(parent, lines, i);
        continue;
      }

      const item = listItem(line);
      if (item && item.indent <= 3) {
        flushParagraph();
        i = renderList(parent, lines, i);
        continue;
      }

      const code = indentedCode(lines, i);
      if (code) {
        flushParagraph();
        codeBlock(parent, code.code, "", { header: true });
        i = code.next;
        continue;
      }

      paragraph.push(line);
      i += 1;

      if (i < lines.length && /^ {0,3}(=+|-+)[ \t]*$/.test(lines[i]) && paragraph.length) {
        const level = lines[i].trim().startsWith("=") ? 1 : 2;
        const content = paragraph.join("\n").trim();
        paragraph = [];
        if (content) {
          const node = el("h" + level, "pi-md-heading");
          renderInline(node, content);
          parent.appendChild(node);
        }
        i += 1;
      }
    }
    flushParagraph();
  }


  function renderText(parent, text) {
    const source = String(text || "");
    const lines = source.split("\n");
    let buffer = [];
    let i = 0;

    function flush() {
      if (!buffer.length) return;
      const chunk = buffer.join("\n");
      buffer = [];
      if (chunk) parent.appendChild(el("div", "pi-text", chunk));
    }

    while (i < lines.length) {
      const block = fence(lines, i);
      if (!block) {
        buffer.push(lines[i]);
        i += 1;
        continue;
      }
      flush();
      codeBlock(parent, block.code, block.lang, { header: true });
      i = block.next;
    }
    flush();
  }

  function guessJson(text) {
    const trimmed = String(text || "").trim();
    if (!trimmed || trimmed.length > 200_000 || !/^[\[{]/.test(trimmed)) return false;
    try {
      JSON.parse(trimmed);
      return true;
    } catch {
      return false;
    }
  }

  function renderCode(parent, text, lang, options) {
    const source = String(text || "");
    const language = normaliseLanguage(lang);
    if (guessJson(source) && (!language || language === "text" || language === "plain")) {
      return codeBlock(parent, source, "json", options);
    }
    if (language === "diff") return codeBlock(parent, source, "diff", options);
    return codeBlock(parent, source, language, options);
  }

  function renderOutput(parent, text, options) {
    const opts = options || {};
    const source = String(text || "");
    if (looksLikeDiff(source)) {
      const pre = el("pre", "pi-pre pi-output pi-diff");
      appendDiff(pre, source);
      parent.appendChild(pre);
      return pre;
    }
    const pre = el("pre", "pi-pre pi-output");
    if (guessJson(source) && opts.json !== false) appendTokens(pre, source, "json");
    else pre.textContent = source;
    parent.appendChild(pre);
    return pre;
  }

  window.WebtermHighlight = {
    renderText,
    renderMarkdown,
    renderCode,
    renderOutput,
    looksLikeDiff,
  };
})();
