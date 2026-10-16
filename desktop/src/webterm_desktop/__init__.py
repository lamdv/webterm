"""PyTauri host for the webterm desktop app.

Step 1: open a Tauri window with a placeholder frontend.  The webterm server is
not started yet; the only goal here is to prove the Tauri/PyTauri runtime works
on this machine.
"""

from __future__ import annotations

from os import environ

# pytauri requires this to be set before any pytauri import.  It selects the
# prebuilt pytauri-wheel native libraries instead of the Cargo extension.
environ["_PYTAURI_DIST"] = "pytauri-wheel"

from pathlib import Path  # noqa: E402

from anyio.from_thread import start_blocking_portal  # noqa: E402
from pytauri import Commands  # noqa: E402
from pytauri_wheel.lib import builder_factory, context_factory  # noqa: E402

#: Directory holding ``Tauri.toml``, ``capabilities/``, ``icons/`` and the
#: frontend assets.  ``context_factory`` reads the Tauri config from here.
PACKAGE_DIR = Path(__file__).parent.absolute()

commands = Commands()


def main() -> int:
    """Run the desktop app and return its exit code."""
    with start_blocking_portal("asyncio") as portal:
        app = builder_factory().build(
            context=context_factory(PACKAGE_DIR),
            invoke_handler=commands.generate_handler(portal),
        )
        return app.run_return()
