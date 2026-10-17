"""Tests for the sub-agent primitives: lineage, waiting, capture, limits."""

from __future__ import annotations

import asyncio
import unittest

from webterm.ansi import remove_ansi
from webterm.session import (
    DepthExceededError,
    SessionLimitError,
    SessionManager,
)


async def wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, failing on timeout."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class AnsiTests(unittest.TestCase):
    def test_removes_csi_and_osc(self) -> None:
        raw = b"\x1b[31mred\x1b[0m plain \x1b]0;title\x07done"
        self.assertEqual(remove_ansi(raw), b"red plain done")

    def test_leaves_plain_text(self) -> None:
        self.assertEqual(remove_ansi(b"hello\n"), b"hello\n")


class SessionPrimitiveTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_wait_returns_exit_code(self) -> None:
        manager = self.manager()
        session = manager.create_single("exit 9")
        self.assertEqual(await session.wait(timeout=5.0), 9)

    async def test_wait_times_out(self) -> None:
        manager = self.manager()
        session = manager.create_single("sleep 30")
        self.assertIsNone(await session.wait(timeout=0.1))
        manager.kill(session.id)

    async def test_capture_and_strip_ansi(self) -> None:
        manager = self.manager()
        session = manager.create_single("printf '\\033[31mhello\\033[0m'")
        await session.wait(timeout=5.0)

        start, end, data = session.capture(0)
        self.assertEqual(start, 0)
        self.assertGreater(end, 0)
        self.assertIn(b"hello", data)

        _, _, clean = session.capture(0, strip_ansi=True)
        self.assertEqual(clean, b"hello")

    async def test_wait_for_text_matches_retained_and_live(self) -> None:
        manager = self.manager()
        finished = manager.create_single("printf 'READY_MARKER\\n'")
        await finished.wait(timeout=5.0)
        matched, _, match = await finished.wait_for_text("READY_MARKER", timeout=1.0)
        self.assertTrue(matched)
        self.assertEqual(match, "READY_MARKER")

        live = manager.create_single("sleep 0.2; printf 'LATE_MARKER\\n'")
        matched, _, match = await live.wait_for_text("LATE_MARKER", timeout=5.0)
        self.assertTrue(matched)
        self.assertEqual(match, "LATE_MARKER")

    async def test_wait_for_text_timeout(self) -> None:
        manager = self.manager()
        session = manager.create_single("sleep 30")
        matched, _, _ = await session.wait_for_text("NOPE", timeout=0.1)
        self.assertFalse(matched)
        manager.kill(session.id)

    async def test_wait_for_text_spans_chunks(self) -> None:
        manager = self.manager()
        session = manager.create_single("printf 'abc'; sleep 0.15; printf 'def\\n'")
        matched, _, match = await session.wait_for_text("abcdef", timeout=5.0)
        self.assertTrue(matched)
        self.assertEqual(match, "abcdef")

    async def test_wait_for_idle(self) -> None:
        manager = self.manager()
        session = manager.create_single("printf 'x'; sleep 0.4")
        self.assertTrue(await session.wait_for_idle(100, timeout=5.0))


class LineageTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_lineage_metadata_and_info(self) -> None:
        manager = self.manager()
        parent = manager.create_single("sleep 30", title="parent")
        child = manager.create_single("sleep 30", parent_id=parent.id, task="child task")

        self.assertEqual(child.parent_id, parent.id)
        self.assertEqual(child.depth, 1)
        self.assertEqual(child.origin, "user")

        info = next(i for i in manager.list_info() if i["id"] == child.id)
        self.assertEqual(info["parent_id"], parent.id)
        self.assertEqual(info["depth"], 1)
        self.assertEqual(info["task"], "child task")
        self.assertIn("created_at", info)

        self.assertEqual([s.id for s in manager.children_of(parent.id)], [child.id])
        self.assertEqual(
            {s.id for s in manager.descendants_of(parent.id)}, {child.id}
        )

    async def test_children_of_grandchildren(self) -> None:
        manager = self.manager()
        a = manager.create_single("sleep 30")
        b = manager.create_single("sleep 30", parent_id=a.id)
        c = manager.create_single("sleep 30", parent_id=b.id)
        self.assertEqual({s.id for s in manager.descendants_of(a.id)}, {b.id, c.id})
        self.assertEqual(c.depth, 2)

    async def test_child_fanout_cap(self) -> None:
        manager = self.manager(max_agent_children=1)
        parent = manager.create_single("sleep 30")
        manager.create_single("sleep 30", parent_id=parent.id)
        with self.assertRaises(SessionLimitError):
            manager.create_single("sleep 30", parent_id=parent.id)

    async def test_depth_guard(self) -> None:
        manager = self.manager(max_agent_depth=1)
        root = manager.create_single("sleep 30")
        child = manager.create_single("sleep 30", parent_id=root.id)
        with self.assertRaises(DepthExceededError):
            manager.create_single("sleep 30", parent_id=child.id)

    async def test_session_cap(self) -> None:
        manager = self.manager(max_sessions=1)
        manager.create_single("sleep 30")
        with self.assertRaises(SessionLimitError):
            manager.create_single("sleep 30")

    async def test_cascade_kill(self) -> None:
        manager = self.manager()
        root = manager.create_single("sleep 30")
        child = manager.create_single("sleep 30", parent_id=root.id)
        grandchild = manager.create_single("sleep 30", parent_id=child.id)

        killed = manager.kill(root.id)
        self.assertEqual(set(killed), {root.id, child.id, grandchild.id})
        self.assertIsNone(manager.get(root.id))
        self.assertIsNone(manager.get(child.id))
        self.assertIsNone(manager.get(grandchild.id))

    async def test_kill_without_tree_orphans_children(self) -> None:
        manager = self.manager()
        root = manager.create_single("sleep 30")
        child = manager.create_single("sleep 30", parent_id=root.id)
        manager.kill(root.id, tree=False)
        self.assertIsNone(manager.get(root.id))
        self.assertIs(manager.get(child.id), child)

    async def test_spawn_tags_agent_role(self) -> None:
        manager = self.manager()
        session = manager.spawn(["sleep", "30"], agent="pi", task="do a thing")
        self.assertEqual(session.role, "agent")
        self.assertEqual(session.origin, "agent")
        self.assertEqual(session.agent, "pi")
        self.assertEqual(session.task, "do a thing")
        self.assertEqual(session.title, "do a thing")

    async def test_private_sessions_are_filtered(self) -> None:
        manager = self.manager()
        manager.create_single("sleep 30", visibility="private")
        manager.create_single("sleep 30", visibility="public")
        self.assertEqual(len(manager.list_info()), 2)
        visible = manager.list_info(include_private=False)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["visibility"], "public")

    async def test_retention_sweep_ttl(self) -> None:
        manager = self.manager(retained_ttl=0.0)
        session = manager.create_single("true")
        await wait_for(lambda: session.dead)
        manager.sweep()
        self.assertIsNone(manager.get(session.id))

    async def test_retention_sweep_cap(self) -> None:
        manager = self.manager(retained_ttl=None, max_retained=1)
        first = manager.create_single("true")
        await wait_for(lambda: first.dead)
        second = manager.create_single("true")
        await wait_for(lambda: second.dead)
        manager.sweep()
        # The oldest finished session is dropped, the newest is kept.
        self.assertIsNone(manager.get(first.id))
        self.assertIs(manager.get(second.id), second)


class ChildEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0.05)

    async def test_control_env_exported(self) -> None:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        manager.set_control_env(socket="/tmp/webterm-test.sock", url="http://127.0.0.1:1")
        try:
            parent = manager.create_single("sleep 30", title="parent")
            child = manager.create_single(
                'printf "%s|%s|%s|%s" "$WEBTERM_SESSION_ID" '
                '"$WEBTERM_PARENT_SESSION" "$WEBTERM_DEPTH" "$WEBTERM_SOCKET"',
                parent_id=parent.id,
            )
            await child.wait(timeout=5.0)
            text = child.capture(0, strip_ansi=True)[2].decode()
            expected = f"{child.id}|{parent.id}|1|/tmp/webterm-test.sock"
            self.assertEqual(text, expected)
        finally:
            manager.close_all()


if __name__ == "__main__":
    unittest.main()
