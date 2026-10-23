/* Composer image attachments: paste, drag/drop, and the thumbnail strip.
 *
 * The controller owns the pending images of one composer.  A view creates it,
 * inserts `element` above its textarea, and controls it with `setEnabled`
 * (capability gating), `images`/`take` (sending) and `clear`.  Images are
 * canonical `{mime_type, data}` entries with raw base64, exactly what the
 * server's `prompt`, `steer` and `follow_up` commands take.
 *
 * The size limits here are advisory: they keep the browser responsive and the
 * error message local.  The server re-checks every image and is the authority
 * (`webterm/agent/protocol.py`).
 */
(() => {
  "use strict";

  const MAX_IMAGES = 4;
  const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
  const MAX_TOTAL_BYTES = 4 * 1024 * 1024;
  const MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function mib(bytes) {
    return String(Math.floor(bytes / (1024 * 1024)));
  }

  function base64Of(bytes) {
    let binary = "";
    // String.fromCharCode runs out of arguments on a multi-megabyte image, so
    // encode in chunks small enough for the argument limit.
    for (let start = 0; start < bytes.length; start += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(start, start + 0x8000));
    }
    return btoa(binary);
  }

  function readData(file) {
    if (file && typeof file.arrayBuffer === "function") {
      return Promise.resolve(file.arrayBuffer()).then((buffer) =>
        base64Of(new Uint8Array(buffer))
      );
    }
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        resolve(comma < 0 ? "" : result.slice(comma + 1));
      };
      reader.onerror = () => reject(reader.error || new Error("could not read image"));
      reader.readAsDataURL(file);
    });
  }

  function decodedBytes(data) {
    const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
    return Math.floor(data.length / 4) * 3 - padding;
  }

  function isImageFile(file) {
    return Boolean(file) && typeof file.type === "string" && file.type.indexOf("image/") === 0;
  }

  function hasFiles(event) {
    const transfer = event && event.dataTransfer;
    if (!transfer) return false;
    const types = Array.from(transfer.types || []);
    if (types.length) return types.indexOf("Files") >= 0;
    return Boolean(transfer.files && transfer.files.length);
  }

  function create(options) {
    options = options || {};
    const form = options.form || null;
    const input = options.input || null;
    const onChange = typeof options.onChange === "function" ? options.onChange : null;
    const onNotice = typeof options.onNotice === "function" ? options.onNotice : null;

    const strip = el("div", "pi-attach-strip");
    strip.hidden = true;

    let enabled = false;
    let images = [];
    // Bumped on every clear so a read that finishes late cannot resurrect a
    // draft the user already sent or removed.
    let generation = 0;

    function notice(text) {
      if (onNotice) onNotice(text);
    }

    function changed() {
      if (onChange) onChange();
    }

    function render() {
      for (const child of Array.from(strip.children || [])) child.remove();
      images.forEach((image, index) => {
        const item = el("div", "pi-attach-item");
        const thumb = el("img", "pi-attach-thumb");
        thumb.alt = "attached image " + (index + 1);
        thumb.src = "data:" + image.mime_type + ";base64," + image.data;
        const remove = el("button", "pi-attach-remove", "\u00d7");
        remove.type = "button";
        remove.title = "Remove image " + (index + 1);
        remove.setAttribute("aria-label", remove.title);
        remove.addEventListener("click", (event) => {
          if (event && event.preventDefault) event.preventDefault();
          images = images.filter((_, at) => at !== index);
          render();
          changed();
        });
        item.append(thumb, remove);
        strip.appendChild(item);
      });
      strip.hidden = !images.length;
    }

    function snapshot() {
      return images.map((image) => ({ mime_type: image.mime_type, data: image.data }));
    }

    function clear() {
      generation += 1;
      if (!images.length) return;
      images = [];
      render();
      changed();
    }

    function addCanonical(entries) {
      if (!enabled || !Array.isArray(entries) || !entries.length) return;
      const next = images.slice();
      for (const entry of entries) {
        if (next.length >= MAX_IMAGES) break;
        if (!entry || typeof entry.mime_type !== "string" || typeof entry.data !== "string") {
          continue;
        }
        if (MIME_TYPES.indexOf(entry.mime_type) < 0 || !entry.data) continue;
        next.push({ mime_type: entry.mime_type, data: entry.data });
      }
      if (next.length === images.length) return;
      images = next;
      render();
      changed();
    }

    function addFiles(fileList) {
      const files = Array.from(fileList || []);
      if (!files.length) return Promise.resolve([]);
      if (!enabled) {
        notice("this agent does not accept images");
        return Promise.resolve([]);
      }
      if (images.length + files.length > MAX_IMAGES) {
        notice("too many images: at most " + MAX_IMAGES + " per message");
        return Promise.resolve([]);
      }
      let total = images.reduce((sum, image) => sum + decodedBytes(image.data), 0);
      const accepted = [];
      for (const file of files) {
        const mime = String((file && file.type) || "");
        if (MIME_TYPES.indexOf(mime) < 0) {
          notice("unsupported image type" + (mime ? ": " + mime : ""));
          return Promise.resolve([]);
        }
        const size = Number((file && file.size) || 0);
        if (size > MAX_IMAGE_BYTES) {
          notice("image too large: at most " + mib(MAX_IMAGE_BYTES) + " MiB each");
          return Promise.resolve([]);
        }
        total += size;
        if (total > MAX_TOTAL_BYTES) {
          notice(
            "images are too large: at most " + mib(MAX_TOTAL_BYTES) + " MiB in total"
          );
          return Promise.resolve([]);
        }
        accepted.push({ file, mime });
      }
      const token = ++generation;
      return Promise.all(accepted.map((entry) => readData(entry.file))).then(
        (data) => {
          if (token !== generation) return [];
          const added = [];
          accepted.forEach((entry, index) => {
            if (!data[index]) return;
            added.push({ mime_type: entry.mime, data: data[index] });
          });
          images = images.concat(added);
          render();
          changed();
          return added;
        },
        (error) => {
          notice(error && error.message ? error.message : "could not read image");
          return [];
        }
      );
    }

    function onPaste(event) {
      const transfer = event && event.clipboardData;
      if (!transfer) return;
      const files = Array.from(transfer.files || []).filter(isImageFile);
      if (!files.length) return;
      if (event.preventDefault) event.preventDefault();
      addFiles(files);
    }

    function onDragOver(event) {
      if (!hasFiles(event)) return;
      if (event.preventDefault) event.preventDefault();
    }

    function onDrop(event) {
      if (!hasFiles(event)) return;
      const transfer = event.dataTransfer;
      const files = Array.from(transfer.files || []).filter(isImageFile);
      if (!files.length) return;
      // Keep the browser from navigating to the dropped file even when this
      // adapter cannot take it.
      if (event.preventDefault) event.preventDefault();
      if (!enabled) {
        notice("this agent does not accept images");
        return;
      }
      addFiles(files);
    }

    if (input) input.addEventListener("paste", onPaste);
    const dropTarget = form || input;
    if (dropTarget) {
      dropTarget.addEventListener("dragover", onDragOver);
      dropTarget.addEventListener("drop", onDrop);
    }

    render();

    return {
      element: strip,
      setEnabled(value) {
        const next = Boolean(value);
        if (next === enabled) return;
        enabled = next;
        if (!enabled && images.length) {
          images = [];
          render();
          changed();
        }
      },
      isEnabled() {
        return enabled;
      },
      images: snapshot,
      count() {
        return images.length;
      },
      take() {
        const taken = snapshot();
        clear();
        return taken;
      },
      add: addCanonical,
      addFiles,
      clear,
    };
  }

  window.WebtermAttach = {
    create,
    MAX_IMAGES,
    MAX_IMAGE_BYTES,
    MAX_TOTAL_BYTES,
    MIME_TYPES,
  };
})();
