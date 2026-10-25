import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from overlay_manager import OverlayManager


class TestOverlayCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orch = SimpleNamespace(control_flags={}, _stop_requested=False,
            _log=Mock(), hitl=SimpleNamespace(resume=Mock()),
            _current_agent_task=Mock())
        self.orch._current_agent_task.done.return_value = False
        self.manager = OverlayManager(self.orch)

    async def test_commands_are_dispatched_in_order_without_losing_suggestions(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=[
            {'action': 'pause'}, {'action': 'suggestion', 'payload': 'Use CSS grid'},
            {'action': 'suggestion', 'payload': 'Keep the heading'}, {'action': 'resume'},
        ]))
        await self.manager.poll_overlay_commands(page)
        self.assertEqual(self.orch.control_flags['suggestions'], ['Use CSS grid', 'Keep the heading'])
        self.assertFalse(self.orch.control_flags['pause_requested'])
        self.orch.hitl.resume.assert_called_once()

    async def test_stop_cancels_work_and_releases_pause(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=[{'action': 'stop'}, {'action': 'pause'}]))
        await self.manager.poll_overlay_commands(page)
        self.assertTrue(self.orch._stop_requested)
        self.assertTrue(self.orch.control_flags['stop_requested'])
        self.assertFalse(self.orch.control_flags['pause_requested'])
        self.orch._current_agent_task.cancel.assert_called_once()
        self.orch.hitl.resume.assert_called_once()

    async def test_legacy_single_command_is_supported(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={'action': 'pause'}))
        await self.manager.poll_overlay_commands(page)
        self.assertTrue(self.orch.control_flags['pause_requested'])

    async def test_serialized_queue_is_supported(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value='[{"action":"suggestion","payload":"hello"}]'))
        await self.manager.poll_overlay_commands(page)
        self.assertEqual(self.orch.control_flags['suggestions'], ['hello'])
