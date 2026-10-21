"""Run the llama adapter as a child process: ``python -m webterm.integration.llama``."""

from __future__ import annotations

from .adapter import main

if __name__ == "__main__":
    raise SystemExit(main())
