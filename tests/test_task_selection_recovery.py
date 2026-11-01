import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from models import CodingTask
from recon_engine import ReconEngine


class TaskSelectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_locator_is_rediscovered_by_normalized_identity(self):
        task = CodingTask()
        task.platform_task.title = 'Part 001'
        task.platform_task.key = '["Chapter B 0%", "part 001"]'
        task.target_selector = '#obsolete'
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=[None, 'Clicked question by identity']))
        orch = SimpleNamespace(browser_mgr=SimpleNamespace(get_browser=AsyncMock()),
            _get_current_page_safe=AsyncMock(return_value=page), _log=Mock(),
            _extract_problem_from_dom_direct=AsyncMock(return_value=('', [
                {'identity': '["Chapter B", "part 001"]', 'selector': '#current'}], [])))
        with patch('recon_engine.asyncio.sleep', new=AsyncMock()), patch('recon_engine.wait_for_page_ready', new=AsyncMock()) as ready:
            await ReconEngine(orch).navigate_to_task_question(task, 2)
        ready.assert_awaited_once_with(page)
        self.assertEqual(task.target_selector, '#current')
        self.assertEqual(page.evaluate.await_args_list[-1].args[1]['taskSelector'], '#current')
        orch._extract_problem_from_dom_direct.assert_awaited_once()
