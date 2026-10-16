// The picker: list the saved servers, add new ones, and open a window per
// server.  All the data lives in Rust (the `ProfileStore`); this file only
// translates between that data and the DOM.

import { invoke } from "./ipc.js";

const errorBox = document.querySelector("#error");
const list = document.querySelector("#servers");
const empty = document.querySelector("#empty");
const form = document.querySelector("#add-form");
const authSelect = document.querySelector("#auth");
const tokenRow = document.querySelector("#token-row");

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

  const connect = document.createElement("button");
  connect.type = "button";
  connect.className = "primary";
  connect.textContent = "Connect";
  connect.addEventListener("click", () => {
    guard(() => invoke("connect", { id: profile.id }));
  });

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => {
    guard(async () => {
      await invoke("remove_profile", { id: profile.id });
      await refresh();
    });
  });

  actions.append(connect, remove);
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

authSelect.addEventListener("change", () => {
  tokenRow.hidden = authSelect.value !== "token";
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    const name = document.querySelector("#name").value.trim();
    const url = document.querySelector("#url").value.trim();
    const kind = authSelect.value;
    const token = document.querySelector("#token").value;
    const auth = kind === "token" ? { kind, token } : { kind };

    await invoke("add_profile", {
      profile: { id: newId(), name, url, auth },
    });

    form.reset();
    tokenRow.hidden = true;
    await refresh();
  });
});

// First paint: ask Rust for the list.
guard(refresh);
