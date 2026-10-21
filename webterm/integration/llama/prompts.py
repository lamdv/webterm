"""A plain-text system-prompt library for the llama chat adapter.

One prompt per file.  A prompt is ``<name>.md`` directly inside a single
directory, UTF-8 plain text, stripped, non-empty, and at most
:data:`MAX_PROMPT_CHARS` characters.  Names are validated so a name can never
address a path outside that directory.  The first use of a missing directory
creates it and seeds a few short starters; an existing directory, empty or
not, is left exactly as it is.

The library is independent of any session: it stores text, not transcripts,
and saving a prompt never changes a session's active system prompt.  The
canonical ``list_prompts``, ``save_prompt``, and ``delete_prompt`` commands are
the only entry points the adapter exposes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import default_prompt_dir

#: File extension of one prompt.  Only these files are listed.
SUFFIX = ".md"
#: Allowed prompt names: no leading dot, no separator, at most 64 characters.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Maximum length of the normalized prompt text.
MAX_PROMPT_CHARS = 16384

#: Starters written the first time a library directory is created.  Existing
#: files are never overwritten and an existing directory is never seeded.
STARTER_PROMPTS: dict[str, str] = {
    "concise": (
        "Answer concisely. Prefer short paragraphs or bullets and stop as soon as the "
        "question is answered."
    ),
    "code-review": (
        "Review the code for correctness, clarity, and risk. Cite the lines you mean and "
        "give one concrete fix per problem.\n"
        "Lead with the most serious issue and skip praise."
    ),
    "explain": (
        "Explain the idea in plain language, then give one short example. Define every term "
        "you introduce.\nEnd with the one thing the reader should remember."
    ),
    "socratic": (
        "Ask the learner one question at a time and wait for the answer. Do not give the "
        "solution away.\nUse hints and smaller questions to lead them there."
    ),
}


class PromptError(ValueError):
    """A prompt library request the caller must fix.

    The message is written for a human: the adapter forwards it as the
    ``error`` of a ``success: false`` response.
    """


def validate_name(name: object) -> str:
    """Return *name* if it is a legal prompt name, else raise :class:`PromptError`."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise PromptError(
            "prompt name must be 1-64 characters of letters, digits, '.', '-', or '_', "
            "and must start with a letter or digit"
        )
    return name


def normalize_text(text: object) -> str:
    """Return the stripped prompt text, validating presence and length."""
    if not isinstance(text, str):
        raise PromptError("prompt text must be a string")
    normalized = text.strip()
    if not normalized:
        raise PromptError("prompt text is empty")
    if len(normalized) > MAX_PROMPT_CHARS:
        raise PromptError(
            f"prompt text is {len(normalized)} characters; the limit is {MAX_PROMPT_CHARS}"
        )
    return normalized


class PromptLibrary:
    """The prompts in one directory, as ``{name, text}`` records.

    Every public method validates its arguments and raises
    :class:`PromptError` on anything the caller can fix (a bad name, empty or
    oversize text, or a directory that cannot be read or written).  Unreadable
    or malformed files are skipped while listing, never fatal.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        """Use *directory*, or the configured default when it is ``None``."""
        self.directory = (
            Path(directory) if directory is not None else default_prompt_dir()
        )

    # ------------------------------------------------------------- internals

    def _path(self, name: str) -> Path:
        """Return the file for a validated *name*, never outside the root."""
        candidate = self.directory / f"{name}{SUFFIX}"
        if candidate.parent != self.directory:
            raise PromptError(f"prompt name {name!r} leaves the prompt directory")
        return candidate

    def _ensure_directory(self) -> None:
        """Create a missing directory and seed it; leave an existing one alone."""
        if self.directory.exists():
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PromptError(
                f"cannot create prompt directory {self.directory}: {exc}"
            ) from exc
        self._seed()

    def _seed(self) -> None:
        """Write the starter prompts that are not already present."""
        for name, text in STARTER_PROMPTS.items():
            path = self._path(name)
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(f"{normalize_text(text)}\n")
            except FileExistsError:
                continue
            except OSError:
                continue

    def _read(self, name: str) -> str:
        """Read one prompt file, raising :class:`PromptError` when it is unusable."""
        path = self._path(name)
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                raw = handle.read(MAX_PROMPT_CHARS + 1)
        except OSError as exc:
            raise PromptError(f"cannot read prompt {name!r}: {exc}") from exc
        return normalize_text(raw)

    # ------------------------------------------------------------------- api

    def list(self) -> list[dict[str, str]]:
        """Return every valid prompt, sorted by name, as ``{name, text}`` records."""
        self._ensure_directory()
        try:
            entries = list(self.directory.iterdir())
        except OSError as exc:
            raise PromptError(
                f"cannot list prompt directory {self.directory}: {exc}"
            ) from exc
        prompts: list[dict[str, str]] = []
        for entry in entries:
            if not entry.name.endswith(SUFFIX):
                continue
            name = entry.name[: -len(SUFFIX)]
            if not NAME_PATTERN.match(name):
                continue
            try:
                text = self._read(name)
            except PromptError:
                continue
            prompts.append({"name": name, "text": text})
        prompts.sort(key=lambda prompt: prompt["name"])
        return prompts

    def save(self, name: object, text: object) -> dict[str, str]:
        """Create or overwrite one prompt and return its normalized record."""
        clean_name = validate_name(name)
        clean_text = normalize_text(text)
        self._ensure_directory()
        try:
            self._path(clean_name).write_text(f"{clean_text}\n", encoding="utf-8")
        except OSError as exc:
            raise PromptError(f"cannot write prompt {clean_name!r}: {exc}") from exc
        return {"name": clean_name, "text": clean_text}

    def delete(self, name: object) -> bool:
        """Delete one prompt; return False when it did not exist."""
        clean_name = validate_name(name)
        self._ensure_directory()
        try:
            self._path(clean_name).unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PromptError(f"cannot delete prompt {clean_name!r}: {exc}") from exc
        return True
