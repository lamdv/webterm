"""Unit tests for PTY session lifecycle: one-shot runs and retention."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from webterm.session import SessionManager, _single_argv


async def wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, failing on timeout."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class SessionRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.managers: list[SessionManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            manager.close_all()
        await asyncio.sleep(0.05)

    def manager(self, **kwargs) -> SessionManager:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp", **kwargs)
        self.managers.append(manager)
        return manager

    async def test_single_run_captures_output_and_exit_code(self) -> None:
        manager = self.manager()
        session = manager.create_single("printf 'SINGLE_MARKER\\n'; exit 3")
        await wait_for(lambda: session.dead)

        self.assertTrue(session.dead)
        self.assertEqual(session.exit_code, 3)
        self.assertIn(b"SINGLE_MARKER", session.buffer)

        # Retained: still addressable after exit, with the finished state.
        self.assertIs(manager.get(session.id), session)
        info = next(i for i in manager.list_info() if i["id"] == session.id)
        self.assertFalse(info["alive"])
        self.assertEqual(info["exit_code"], 3)

    async def test_single_run_accepts_argv(self) -> None:
        manager = self.manager()
        session = manager.create_single(["/bin/echo", "ARGV_OK"])
        await wait_for(lambda: session.dead)

        self.assertEqual(session.exit_code, 0)
        self.assertIn(b"ARGV_OK", session.buffer)

    async def test_single_run_drains_large_output_before_teardown(self) -> None:
        # A fast, high-volume command: the final bytes must not be lost when the
        # child exits before the readable callback has caught up.
        manager = self.manager(scrollback=1_000_000)
        session = manager.create_single("seq 1 20000; printf '\\nDRAIN_END\\n'")
        await wait_for(lambda: session.dead, timeout=10.0)

        self.assertIn(b"DRAIN_END", session.buffer)
        self.assertGreater(session.total, 100_000)

    async def test_single_run_can_be_replayed_after_exit(self) -> None:
        manager = self.manager()
        session = manager.create_single("printf 'REPLAY_ME\\n'")
        await wait_for(lambda: session.dead)

        start, data = session.replay_since(0)
        self.assertEqual(start, 0)
        self.assertIn(b"REPLAY_ME", data)

    async def test_single_run_without_output_still_exits(self) -> None:
        manager = self.manager()
        session = manager.create_single("exit 42")
        await wait_for(lambda: session.dead)

        self.assertEqual(session.exit_code, 42)

    async def test_interactive_session_is_removed_on_exit(self) -> None:
        # A plain interactive shell was never marked for retention.
        manager = self.manager()
        session = manager.create(command="exit 7")
        await wait_for(lambda: manager.get(session.id) is None)

        self.assertTrue(session.dead)
        self.assertEqual(session.exit_code, 7)

    async def test_kill_finished_run_drops_it(self) -> None:
        manager = self.manager()
        session = manager.create_single("true")
        await wait_for(lambda: session.dead)

        self.assertTrue(manager.kill(session.id))
        self.assertIsNone(manager.get(session.id))

    async def test_kill_live_run_is_not_retained(self) -> None:
        manager = self.manager()
        session = manager.create_single("sleep 30")
        self.assertTrue(manager.kill(session.id))
        await wait_for(lambda: manager.get(session.id) is None)

        self.assertTrue(session.dead)

    async def test_fallback_reaper_when_signals_unavailable(self) -> None:
        # Off-main-thread loops (the Tauri desktop app) cannot install a SIGCHLD
        # handler; the poll reaper must still reap children and report exits.
        manager = self.manager()
        loop = asyncio.get_running_loop()
        with mock.patch.object(
            type(loop), "add_signal_handler", side_effect=ValueError("worker thread")
        ):
            session = manager.create_single("exit 5")

        self.assertIsNotNone(manager._reap_task)
        await wait_for(lambda: session.dead)
        self.assertEqual(session.exit_code, 5)


class SingleArgvTests(unittest.TestCase):
    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _single_argv("/bin/sh", "")
        with self.assertRaises(ValueError):
            _single_argv("/bin/sh", [])

    def test_string_runs_via_shell(self) -> None:
        self.assertEqual(_single_argv("/bin/sh", "echo hi"), ["/bin/sh", "-c", "echo hi"])

    def test_argv_is_executed_directly(self) -> None:
        self.assertEqual(_single_argv("/bin/sh", ["echo", "hi"]), ["echo", "hi"])


if __name__ == "__main__":
    unittest.main()
