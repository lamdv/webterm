// The picker: list the saved servers, add, edit and remove them, probe each
// one's status, and navigate to a server.  All the data lives in Rust (the
// `ProfileStore`); this file only translates between that data and the DOM.

import { invoke } from "./ipc.js";

const errorBox = document.querySelector("#error");
const list = document.querySelector("#servers");
const empty = document.querySelector("#empty");
const form = document.querySelector("#add-form");
const authSelect = document.querySelector("#auth");
const tokenRow = document.querySelector("#token-row");
const submitButton = document.querySelector("#submit");
const cancelButton = document.querySelector("#cancel");

/// The saved profiles, the latest probe result per id, and the id being
/// edited (`null` when adding).  Module-level because the form and the list
/// both need to see them.
let profiles = [];
let probes = new Map();
let editingId = null;

function showError(error) {
  errorBox.textContent = error instanceof Error ? error.message : String(error);
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

/// Run an async action, surfacing any rejection in the error banner.
async function guard(action) {
  clearError();
  try {
    await action();
  } catch (error) {
    showError(error);
  }
}

/// A stable id for a new profile.  `randomUUID` exists in every modern webview;
/// the fallback keeps the picker usable if one ever lacks it.
function newId() {
  if (crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `server-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

/// The human label for an `AuthKind`, mirroring `AuthKind::label` in Rust.
function authLabel(auth) {
  switch (auth?.kind) {
    case "token":
      return "token";
    case "browser":
      return "browser login";
    default:
      return "no auth";
  }
}

/// What the auth pill means, shown on hover.
function authTitle(auth) {
  switch (auth?.kind) {
    case "token":
      return "Authenticates to the server with a shared token";
    case "browser":
      return "Signs in through an identity-aware proxy (oauth2-proxy, Authelia)";
    default:
      return "No credential: the server is open, or the network protects it";
  }
}

/// Map a probe result (or its absence) to a status pill.
function probeState(probe) {
  if (!probe) {
    return { className: "checking", label: "checking…" };
  }
  switch (probe.status) {
    case "online": {
      const version = probe.server?.version;
      return { className: "online", label: version ? `v${version}` : "online" };
    }
    case "incompatible": {
      const protocol = probe.server?.protocol;
      return {
        className: "warn",
        label: protocol ? `needs protocol ${protocol}` : "incompatible",
      };
    }
    case "unknown":
      // Reachable, but no discovery document: usually an older server.
      return { className: "unknown", label: "no discovery" };
    default:
      return { className: "offline", label: "offline" };
  }
}

function button(label, className, onClick) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = label;
  element.addEventListener("click", onClick);
  return element;
}

function serverRow(profile) {
  const item = document.createElement("li");
  item.className = "server";

  const text = document.createElement("div");
  text.className = "server-text";

  const name = document.createElement("span");
  name.className = "server-name";
  name.textContent = profile.name;

  const url = document.createElement("span");
  url.className = "server-url";
  url.textContent = profile.url;
  url.title = profile.url;

  text.append(name, url);

  const probe = probes.get(profile.id);
  const state = probeState(probe);
  const status = document.createElement("span");
  status.className = `server-status ${state.className}`;
  status.textContent = state.label;
  if (probe?.error) {
    // The failure reason is one hover away, without cluttering the row.
    status.title = probe.error;
  }

  const badge = document.createElement("span");
  badge.className = "server-auth";
  badge.textContent = authLabel(profile.auth);
  badge.title = authTitle(profile.auth);

  const actions = document.createElement("div");
  actions.className = "server-actions";

  const connect = button("Connect", "primary", () => {
    guard(() => invoke("connect", { id: profile.id }));
  });
  const edit = button("Edit", "", () => startEdit(profile));
  const remove = button("Remove", "danger", () => {
    guard(async () => {
      await invoke("remove_profile", { id: profile.id });
      if (editingId === profile.id) {
        stopEdit();
      }
      await load();
    });
  });

  actions.append(connect, edit, remove);
  item.append(text, status, badge, actions);
  return item;
}

function render() {
  // `replaceChildren` with DOM nodes (never HTML strings) means a profile
  // named `<img onerror=...>` is shown as text, not executed.
  list.replaceChildren(...profiles.map(serverRow));
  empty.hidden = profiles.length > 0;
}

/// Fetch the list from Rust, render it, then probe each server.
async function load() {
  profiles = await invoke("list_profiles");
  render();
  await guard(probeAll);
}

/// Ask Rust for every server's status and update the pills.
async function probeAll() {
  const results = await invoke("probe_all");
  probes = new Map(results.map((result) => [result.id, result]));
  render();
}

/// Load a profile into the form and switch the form into "save" mode.
function startEdit(profile) {
  editingId = profile.id;
  document.querySelector("#name").value = profile.name;
  document.querySelector("#url").value = profile.url;
  const kind = profile.auth?.kind ?? "none";
  authSelect.value = kind;
  tokenRow.hidden = kind !== "token";
  document.querySelector("#token").value = profile.auth?.token ?? "";
  submitButton.textContent = "Save changes";
  cancelButton.hidden = false;
  document.querySelector("#name").focus();
}

/// Leave edit mode and clear the form back to "add".
function stopEdit() {
  editingId = null;
  form.reset();
  tokenRow.hidden = true;
  submitButton.textContent = "Add server";
  cancelButton.hidden = true;
}

authSelect.addEventListener("change", () => {
  tokenRow.hidden = authSelect.value !== "token";
});

cancelButton.addEventListener("click", stopEdit);

document
  .querySelector("#refresh")
  .addEventListener("click", () => guard(load));

form.addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    const name = document.querySelector("#name").value.trim();
    const url = document.querySelector("#url").value.trim();
    const kind = authSelect.value;
    const token = document.querySelector("#token").value;
    const auth = kind === "token" ? { kind, token } : { kind };

    // An edit keeps its id; a new profile gets a fresh one.
    const profile = { id: editingId ?? newId(), name, url, auth };
    if (editingId === null) {
      await invoke("add_profile", { profile });
    } else {
      await invoke("update_profile", { profile });
    }

    stopEdit();
    await load();
  });
});

// First paint: ask Rust for the list, then probe.
guard(load);
