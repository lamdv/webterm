"""A canonical agent adapter for llama-server's OpenAI-compatible chat API.

The process speaks webterm's JSONL protocol on stdin/stdout; prompts are
streamed from ``POST /v1/chat/completions`` and translated into canonical
events.  It is intentionally chat-only: llama-server does not provide tools,
shell access, or an agent loop, so the adapter advertises none of those
capabilities.  The transcript lives in this process's memory for the lifetime
of the session; there is no resume or export.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

#: Canonical protocol version announced in ``hello``.
PROTOCOL_VERSION = 1
#: Default provider label used in model records.
DEFAULT_PROVIDER = "llama"
#: Socket timeout for one completion request.  Streaming can pause while the
#: server loads a model, so this is deliberately generous.
DEFAULT_TIMEOUT = 600.0

CAPABILITIES = {
    "prompt": True,
    "abort": True,
    "steering": False,
    "follow_up": False,
    "queue": False,
    "dialogs": [],
    "models": {"list": True, "set": True, "thinking": False},
    "snapshot": True,
    "history": {"paging": True},
    "resume": False,
    "export": {"native": False},
    "usage": {"tokens": True, "cost": False},
    "tools": {"streaming": False, "updates": False},
    "attachments": {"images": False},
    "raw": True,
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _error_detail(exc: BaseException) -> str:
    """Prefer the server's JSON error body over urllib's status line."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - the response may already be closed
            body = ""
        return f"HTTP {exc.code}: {body or exc.reason}"
    return str(exc)


def _is_tls_failure(exc: BaseException) -> bool:
    """True when the request failed during the TLS handshake itself.

    A plain-HTTP server reached over ``https://`` answers the ClientHello with
    HTTP bytes, which surfaces as ``WRONG_VERSION_NUMBER`` or
    ``RECORD_LAYER_FAILURE`` from OpenSSL rather than a clean connection error.
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, ssl.SSLError)


def _text_of(entry: dict[str, Any]) -> str:
    content = entry.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class LlamaAgent:
    """One chat session against a llama-server or llama-swap base URL."""

    def __init__(
        self,
        url: str,
        *,
        model: str | None = None,
        provider: str = DEFAULT_PROVIDER,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.entries: list[dict[str, Any]] = []
        self.streaming = False
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._abort = threading.Event()
        self._response: Any = None
        self._entry_counter = 0
        self._run_counter = 0
        self._running = False
        self._stopping = False

    # ------------------------------------------------------------------ wire

    def send(self, record: dict[str, Any]) -> None:
        """Write one protocol record; callers from any thread may use this."""
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                sys.stdout.write(payload)
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                self._stopping = True

    def respond(
        self,
        command_id: object,
        command: str,
        data: dict[str, Any] | None = None,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "type": "response",
            "command": command,
            "success": success,
        }
        if command_id is not None:
            record["id"] = command_id
        if error is not None:
            record["error"] = error
        else:
            record["data"] = data or {}
        self.send(record)

    def event(self, name: str, **fields: Any) -> None:
        self.send({"type": "event", "event": name, **fields})

    def hello(self) -> None:
        self.send(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "agent": "llama",
                "version": "0.1.0",
                "capabilities": CAPABILITIES,
            }
        )
        if self.model:
            self.event("session.info", model=self.model, provider=self.provider)

    # -------------------------------------------------------------- transcript

    def _add_entry(self, role: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        with self._state_lock:
            self._entry_counter += 1
            entry = {
                "id": f"entry-{self._entry_counter}",
                "role": role,
                "content": content,
                "timestamp": _now(),
            }
            self.entries.append(entry)
            return entry

    def _messages(self) -> list[dict[str, str]]:
        with self._state_lock:
            entries = list(self.entries)
        messages: list[dict[str, str]] = []
        for entry in entries:
            text = _text_of(entry)
            if text:
                messages.append({"role": str(entry.get("role", "user")), "content": text})
        return messages

    def _snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            entries = list(self.entries)
        return {
            "entries": entries,
            "state": {
                "model": self.model,
                "provider": self.provider,
                "streaming": self.streaming,
            },
        }

    def _detail(self, exc: BaseException) -> str:
        detail = _error_detail(exc)
        if self.url.startswith("https:") and _is_tls_failure(exc):
            detail += (
                "; the endpoint did not complete a TLS handshake, and llama-server and"
                " llama-swap serve plain HTTP, so use http:// unless a TLS proxy fronts them"
            )
        return detail

    # -------------------------------------------------------------------- HTTP

    def _open(self, path: str, payload: dict[str, Any] | None = None, *, timeout: float):
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            headers=headers,
            method="POST" if data else "GET",
        )
        return urllib.request.urlopen(request, timeout=timeout)

    def fetch_models(self) -> list[dict[str, Any]]:
        """Return the server's models in canonical model-record shape."""
        with self._open("/v1/models", timeout=10.0) as response:
            payload = json.load(response)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = []
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id") or item.get("model") or item.get("name")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                continue
            seen.add(identifier)
            record: dict[str, Any] = {"id": identifier, "provider": self.provider}
            name = item.get("name")
            if isinstance(name, str) and name:
                record["name"] = name
            models.append(record)
        return models

    def _ensure_model(self) -> str:
        with self._state_lock:
            if self.model:
                return self.model
        models = self.fetch_models()
        if not models:
            raise RuntimeError("the llama server reported no models")
        self.model = str(models[0]["id"])
        self.event("session.info", model=self.model, provider=self.provider)
        return self.model

    # ----------------------------------------------------------------- commands

    def prompt(self, command: dict[str, Any]) -> None:
        command_id = command.get("id")
        message = command.get("message")
        if not isinstance(message, str) or not message.strip():
            self.respond(command_id, "prompt", success=False, error="prompt message is empty")
            return
        with self._state_lock:
            if self._running:
                busy = True
            else:
                busy = False
                self._running = True
                self.streaming = True
                self._run_counter += 1
                run_number = self._run_counter
        if busy:
            self.respond(
                command_id, "prompt", success=False, error="a run is already active"
            )
            return
        try:
            self._ensure_model()
        except Exception as exc:  # noqa: BLE001 - report the server's failure
            with self._state_lock:
                self._running = False
                self.streaming = False
            self.respond(command_id, "prompt", success=False, error=self._detail(exc))
            return
        self._abort.clear()
        self._add_entry("user", [{"type": "text", "text": message}])
        run_id = f"run-{run_number}"
        message_id = f"message-{run_number}"
        self.respond(command_id, "prompt", {"disposition": "started"})
        threading.Thread(
            target=self._run,
            args=(run_id, message_id),
            name=f"llama-{run_id}",
            daemon=True,
        ).start()

    def _run(self, run_id: str, message_id: str) -> None:
        text: list[str] = []
        thinking: list[str] = []
        seen_thinking = False
        usage: dict[str, Any] | None = None
        reason = "stop"
        self.event("run.started", run_id=run_id)
        self.event("message.started", message={"id": message_id, "role": "assistant"})
        try:
            payload = {
                "model": self.model,
                "messages": self._messages(),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            response = self._open("/v1/chat/completions", payload, timeout=self.timeout)
            self._response = response
            try:
                while not self._abort.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices")
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            delta = {}
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            seen_thinking = True
                            thinking.append(reasoning)
                            self.event(
                                "message.delta",
                                message_id=message_id,
                                block_index=0,
                                block_type="thinking",
                                delta=reasoning,
                            )
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            text.append(content)
                            self.event(
                                "message.delta",
                                message_id=message_id,
                                block_index=1 if seen_thinking else 0,
                                block_type="text",
                                delta=content,
                            )
            finally:
                self._response = None
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
            if self._abort.is_set():
                reason = "aborted"
        except Exception as exc:  # noqa: BLE001 - the session must survive
            if self._abort.is_set():
                reason = "aborted"
            else:
                reason = "error"
                self.event(
                    "notice",
                    level="error",
                    code="llama_error",
                    text=f"llama server request failed: {self._detail(exc)}",
                )
        finally:
            blocks: list[dict[str, Any]] = []
            if thinking:
                blocks.append({"type": "thinking", "text": "".join(thinking)})
            if text:
                blocks.append({"type": "text", "text": "".join(text)})
            entry = self._add_entry("assistant", blocks)
            self.event(
                "message.finished",
                message={
                    "id": message_id,
                    "role": "assistant",
                    "content": entry["content"],
                },
            )
            if usage is not None:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                if not isinstance(total_tokens, int) or isinstance(total_tokens, bool):
                    total_tokens = prompt_tokens + completion_tokens
                self.event(
                    "usage",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            self.event("run.finished", run_id=run_id, reason=reason)
            with self._state_lock:
                self._running = False
                self.streaming = False
            self.event("run.settled", run_id=run_id)

    def abort(self, command: dict[str, Any]) -> None:
        self._abort.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self.respond(command.get("id"), "abort", {"ok": True})

    def get_state(self, command: dict[str, Any]) -> None:
        self.respond(
            command.get("id"),
            "get_state",
            {
                "model": self.model,
                "provider": self.provider,
                "streaming": self.streaming,
                "session_file": None,
            },
        )

    def get_snapshot(self, command: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        entries = snapshot["entries"]
        limit = command.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
            page = entries[-limit:] if limit else []
        else:
            page = entries
        snapshot["entries"] = page
        snapshot["has_more"] = len(page) < len(entries)
        snapshot["first_entry_id"] = page[0]["id"] if page else None
        self.respond(command.get("id"), "get_snapshot", snapshot)

    def get_history(self, command: dict[str, Any]) -> None:
        with self._state_lock:
            entries = list(self.entries)
        before = command.get("before")
        limit = command.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = 100
        index = len(entries)
        if isinstance(before, str) and before:
            for position, entry in enumerate(entries):
                if entry["id"] == before:
                    index = position
                    break
        start = max(0, index - limit)
        page = entries[start:index]
        self.respond(
            command.get("id"),
            "get_history",
            {
                "entries": page,
                "has_more": start > 0,
                "first_entry_id": page[0]["id"] if page else None,
            },
        )

    def list_models(self, command: dict[str, Any]) -> None:
        try:
            models = self.fetch_models()
        except Exception as exc:  # noqa: BLE001 - the caller gets the reason
            self.respond(
                command.get("id"),
                "list_models",
                success=False,
                error=self._detail(exc),
            )
            return
        default = self.model
        if not default and models:
            default = models[0]["id"]
        self.respond(
            command.get("id"),
            "list_models",
            {"models": models, "default": default},
        )

    def set_model(self, command: dict[str, Any]) -> None:
        model = command.get("model")
        if not isinstance(model, str) or not model.strip():
            self.respond(
                command.get("id"), "set_model", success=False, error="model is required"
            )
            return
        self.model = model.strip()
        self.event("session.info", model=self.model, provider=self.provider)
        self.respond(command.get("id"), "set_model", {"model": self.model})

    def raw(self, command: dict[str, Any]) -> None:
        inner = command.get("command")
        if not isinstance(inner, dict):
            self.respond(command.get("id"), "raw", success=False, error="raw needs a command")
            return
        name = inner.get("type")
        if name == "get_last_assistant_text":
            with self._state_lock:
                entries = list(self.entries)
            text = ""
            for entry in reversed(entries):
                if entry.get("role") == "assistant":
                    text = _text_of(entry)
                    break
            self.respond(command.get("id"), "raw", {"text": text})
        elif name == "get_session_stats":
            self.respond(
                command.get("id"),
                "raw",
                {"model": self.model, "provider": self.provider},
            )
        else:
            self.respond(
                command.get("id"),
                "raw",
                success=False,
                error=f"unsupported raw command {name!r}",
            )

    def shutdown(self, command: dict[str, Any]) -> None:
        self._abort.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self.respond(command.get("id"), "shutdown", {"ok": True})
        self._stopping = True

    # -------------------------------------------------------------------- loop

    def handle(self, command: dict[str, Any]) -> None:
        name = command.get("type")
        handlers = {
            "prompt": self.prompt,
            "abort": self.abort,
            "get_state": self.get_state,
            "get_snapshot": self.get_snapshot,
            "get_history": self.get_history,
            "list_models": self.list_models,
            "set_model": self.set_model,
            "raw": self.raw,
            "shutdown": self.shutdown,
        }
        handler = handlers.get(str(name))
        if handler is None:
            self.respond(
                command.get("id"), str(name), success=False, error=f"unsupported command {name!r}"
            )
            return
        try:
            handler(command)
        except Exception as exc:  # noqa: BLE001 - never kill the session on a command
            self.respond(command.get("id"), str(name), success=False, error=str(exc))

    def run(self) -> None:
        self.hello()
        while not self._stopping:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(command, dict):
                continue
            self.handle(command)
        self._abort.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webterm-llama-agent",
        description="Expose a llama-server / llama-swap chat model as a webterm agent.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("WEBTERM_LLAMA_URL") or os.environ.get("LLAMA_SWAP_URL"),
    )
    parser.add_argument("--model", default=os.environ.get("WEBTERM_LLAMA_MODEL"))
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url:
        print(
            "webterm llama agent: --url or WEBTERM_LLAMA_URL is required",
            file=sys.stderr,
        )
        return 2
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print(
            f"webterm llama agent: invalid URL {args.url!r}",
            file=sys.stderr,
        )
        return 2
    agent = LlamaAgent(
        args.url,
        model=args.model,
        provider=args.provider,
        timeout=args.timeout,
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
