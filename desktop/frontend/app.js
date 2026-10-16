// The picker: list the saved servers, add, edit and remove them, and open a
// window per server.  All the data lives in Rust (the `ProfileStore`); this
// file only translates between that data and the DOM.

import { invoke } from "./ipc.js";

const errorBox = document.querySelector("#error");
const list = document.querySelector("#servers");
const empty = document.querySelector("#empty");
const form = document.querySelector("#add-form");
const authSelect = document.querySelector("#auth");
const tokenRow = document.querySelector("#token-row");
const submitButton = document.querySelector("#submit");
const cancelButton = document.querySelector("#cancel");

/// The id being edited, or `null` when the form is adding a new profile.
/// Module-level because the form and the list both need to see it.
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
      return "open";
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

  text.append(name, url);

  const badge = document.createElement("span");
  badge.className = "server-auth";
  badge.textContent = authLabel(profile.auth);

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
      await refresh();
    });
  });

  actions.append(connect, edit, remove);
  item.append(text, badge, actions);
  return item;
}

async function refresh() {
  const profiles = await invoke("list_profiles");
  // `replaceChildren` with DOM nodes (never HTML strings) means a profile
  // named `<img onerror=...>` is shown as text, not executed.
  list.replaceChildren(...profiles.map(serverRow));
  empty.hidden = profiles.length > 0;
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
    await refresh();
  });
});

// First paint: ask Rust for the list.
guard(refresh);
