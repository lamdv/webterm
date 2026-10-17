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
      const match = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(lines[i]);
      if (!match) {
        buffer.push(lines[i]);
        i += 1;
        continue;
      }
      flush();
      const marker = match[1][0];
      const length = match[1].length;
      const lang = normaliseLanguage(match[2]);
      const code = [];
      i += 1;
      const closing = new RegExp("^ {0,3}" + marker + "{" + length + ",}[ \\t]*$");
      while (i < lines.length && !closing.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      codeBlock(parent, code.join("\n"), lang, { header: true });
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
    renderCode,
    renderOutput,
    looksLikeDiff,
  };
})();
