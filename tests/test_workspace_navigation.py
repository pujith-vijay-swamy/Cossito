"""Workspace recovery must not confuse a safe reference host with a task."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from agent_core import AgentOrchestrator
from models import AgentPhase
from prompts import get_execution_task_prompt, get_execution_system_prompt


class TestWorkspaceNavigation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orch = AgentOrchestrator.__new__(AgentOrchestrator)
        self.orch.url = 'http://localhost:3000/course'
        self.orch._workspace_url = 'http://localhost:3000/course?task=second#editor'
        self.orch._reference_urls = {'http://localhost:3000/assets/mockup.png'}
        self.orch._workspace_lock = asyncio.Lock()
        self.orch.hitl = SimpleNamespace(is_paused=False)
        self.orch._log = Mock()

    def page(self, url):
        page = SimpleNamespace(url=url)
        page.get_url = AsyncMock(side_effect=lambda: page.url)
        async def goto(destination):
            page.url = destination
        page.goto = AsyncMock(side_effect=goto)
        return page

    async def test_external_reference_returns_to_selected_task_not_course_root(self):
        page = self.page('https://reference.example/images/mockup')
        self.assertTrue(await self.orch._restore_workspace_if_needed(page))
        page.goto.assert_awaited_once_with(self.orch._workspace_url)
        self.assertEqual(self.orch.url, 'http://localhost:3000/course')

    async def test_same_origin_image_is_also_restored(self):
        page = self.page('http://localhost:3000/assets/mockup.png')
        self.assertTrue(await self.orch._restore_workspace_if_needed(page))

    async def test_recovery_reselects_task_when_platform_reuses_one_url(self):
        task = object()
        self.orch._current_task = task
        self.orch.state = SimpleNamespace(task_queue=SimpleNamespace(tasks=[object(), task]))
        self.orch._navigate_to_task_question = AsyncMock()
        page = self.page('https://reference.example/')
        self.assertTrue(await self.orch._restore_workspace_if_needed(page))
        self.orch._navigate_to_task_question.assert_awaited_once_with(task, 2)

    async def test_workspace_navigation_is_not_reloaded(self):
        page = self.page('http://localhost:3000/course?task=third')
        self.assertFalse(await self.orch._restore_workspace_if_needed(page))
        page.goto.assert_not_awaited()

    async def test_different_local_port_is_not_the_workspace(self):
        self.assertFalse(self.orch._is_workspace_url('http://localhost:4000/course'))
        self.assertFalse(self.orch._is_workspace_url('https://localhost:3000/course'))

    async def test_human_login_is_not_interrupted(self):
        self.orch.hitl.is_paused = True
        page = self.page('https://identity.example/login')
        self.assertFalse(await self.orch._restore_workspace_if_needed(page))
        page.goto.assert_not_awaited()

    async def test_failed_restore_does_not_allow_editing_external_page(self):
        page = self.page('https://reference.example/')
        page.goto = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'refusing to edit'):
            await self.orch._restore_workspace_if_needed(page)

    async def check_step(self, observed_url, action):
        page = self.page(observed_url)
        browser = SimpleNamespace(get_pages=AsyncMock(return_value=[page]))
        self.orch.browser_mgr = SimpleNamespace(get_browser=AsyncMock(return_value=browser))
        self.orch._get_current_page_safe = AsyncMock(return_value=page)
        self.orch._disable_cdp_debugger_pauses = AsyncMock()
        self.orch.state = SimpleNamespace(phase=AgentPhase.EXECUTING)
        self.orch._last_report = None
        # Stop after the navigation guard so this tests the real callback without
        # starting unrelated overlay/UI work.
        self.orch.control_flags = {'stop_requested': True}
        output = SimpleNamespace(action=[action])
        with self.assertRaises(asyncio.CancelledError):
            await self.orch._on_agent_step(SimpleNamespace(url=observed_url), output)
        return page, output

    async def test_step_blocks_reference_navigation_before_action(self):
        page, output = await self.check_step(self.orch._workspace_url, {
            'navigate': {'url': 'https://docs.example/tutorial'},
        })
        self.assertEqual(output.action, [])
        page.goto.assert_not_awaited()

    async def test_step_discards_stale_click_after_restoring_workspace(self):
        page, output = await self.check_step('https://assets.example/image.png', {
            'click': {'index': 7},
        })
        self.assertEqual(output.action, [])
        page.goto.assert_awaited_once_with(self.orch._workspace_url)


class TestTaskNavigationPrompts(unittest.TestCase):
    def test_multitasking_does_not_request_a_second_next_click(self):
        prompt = get_execution_task_prompt(2, 'Build a card', '', '', total_tasks=3)
        self.assertIn('already selected', prompt)
        self.assertNotIn('FIRST, navigate', prompt)

    def test_asset_url_is_preserved_as_code_data(self):
        url = 'https://assets.example/picture?id=42'
        prompt = get_execution_system_prompt(f'Use {url} in an img', 1, False)
        self.assertIn(url, prompt)
        self.assertIn('URLs inside the problem statement are task data', prompt)
