"""Do not spend the result-wait budget on a submission that never started."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from controller_actions import build_controller
from models import CodingTask, WorkspaceSnapshot, SubmissionRecord


class TestSubmissionActions(unittest.IsolatedAsyncioTestCase):
    async def check_not_started(self, write_ok, click_ok):
        task = CodingTask()
        orch = SimpleNamespace(
            control_flags={}, _current_task=task, _log=Mock(), _notify=Mock(),
            state=SimpleNamespace(current_solution_code='', current_task_index=0),
            browser_mgr=SimpleNamespace(get_browser=AsyncMock(return_value=object())),
            _get_current_page_safe=AsyncMock(return_value=object()),
            _inject_code_direct=AsyncMock(return_value=None),
        )
        adapter = SimpleNamespace(click_submit=AsyncMock(return_value=click_ok), parse_result=AsyncMock(return_value=SubmissionRecord()))
        with patch('controller_actions._get_sym', side_effect=lambda name, fallback: fallback), \
             patch('controller_actions.safe_write', new=AsyncMock(return_value=(write_ok, WorkspaceSnapshot(), ''))), \
             patch('controller_actions.detect_platform', new=AsyncMock(return_value=adapter)), \
             patch('controller_actions.wait_for_terminal', new=AsyncMock()) as wait:
            controller = build_controller(orch)
            action = controller.registry.registry.actions['submit_code_solution'].function
            message = await action(code='print(1)')
            wait.assert_not_awaited()
            self.assertEqual(task.submissions, [])
            return message, adapter

    async def test_failed_write_does_not_submit_or_wait(self):
        message, adapter = await self.check_not_started(False, True)
        self.assertIn('Submission was not attempted', message)
        adapter.click_submit.assert_not_awaited()

    async def test_missing_submit_control_does_not_wait_for_results(self):
        message, adapter = await self.check_not_started(True, False)
        self.assertIn('Submission was not started', message)
        adapter.click_submit.assert_awaited_once()
