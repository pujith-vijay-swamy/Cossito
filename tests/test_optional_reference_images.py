"""Reference images are optional and must not hold up text-only UI tasks."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_core import AgentOrchestrator
from prompts import get_execution_system_prompt, get_recon_task_prompt
from dom_extraction import extract_ref_images_to_disk
from models import CodingTask
from task_subagent import TaskSubagent


class TestOptionalReferenceImages(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orch = AgentOrchestrator.__new__(AgentOrchestrator)
        self.orch._current_task = SimpleNamespace(task_id='first', description='Build an HTML form')
        self.orch._reference_image_cache = {}
        self.orch.browser_mgr = SimpleNamespace(get_browser=AsyncMock(return_value=object()))
        self.orch._get_current_page_safe = AsyncMock(return_value=object())
        self.orch._log = Mock()
        async def hidden(page, extract):
            return await extract()
        self.orch._with_overlay_hidden = AsyncMock(side_effect=hidden)

    async def test_empty_result_is_cached_instead_of_repeatedly_scanning(self):
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(return_value=[])) as extract:
            self.assertEqual(await self.orch._extract_ref_images_to_disk(), [])
            self.assertEqual(await self.orch._extract_ref_images_to_disk(), [])
            extract.assert_awaited_once()

    async def test_available_images_remain_available_to_the_agent(self):
        images = [{'file_path': '/mock/reference.png'}]
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(return_value=images)) as extract:
            self.assertEqual(await self.orch._extract_ref_images_to_disk(), images)
            self.assertEqual(await self.orch._extract_ref_images_to_disk(), images)
            extract.assert_awaited_once()

    async def test_next_task_gets_its_own_image_check(self):
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(return_value=[])) as extract:
            await self.orch._extract_ref_images_to_disk()
            self.orch._current_task.task_id = 'second'
            await self.orch._extract_ref_images_to_disk()
            self.assertEqual(extract.await_count, 2)

    async def test_changed_statement_is_rechecked(self):
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(return_value=[])) as extract:
            await self.orch._extract_ref_images_to_disk()
            self.orch._current_task.description += ' using the newly supplied image'
            await self.orch._extract_ref_images_to_disk()
            self.assertEqual(extract.await_count, 2)

    async def test_extraction_failure_does_not_block_coding(self):
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(side_effect=RuntimeError('unavailable'))):
            self.assertEqual(await self.orch._extract_ref_images_to_disk(), [])

    async def test_timeout_returns_optional_empty_result(self):
        async def timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()
        with patch('agent_core.asyncio.wait_for', side_effect=timeout) as wait:
            self.assertEqual(await self.orch._extract_ref_images_to_disk(timeout_seconds=0.01), [])
            self.assertEqual(wait.call_args.kwargs['timeout'], 0.01)

    async def test_user_cancellation_is_not_swallowed(self):
        with patch('agent_core.extract_ref_images_to_disk', new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.orch._extract_ref_images_to_disk()
        self.assertEqual(self.orch._reference_image_cache, {})

    async def test_empty_scan_has_no_overlay_cleanup_or_second_browser_call(self):
        for response in ('[]', []):
            page = SimpleNamespace(evaluate=AsyncMock(return_value=response))
            capture_context = AsyncMock()
            result = await extract_ref_images_to_disk(None, page, capture_context=capture_context)
            self.assertEqual(result, [])
            page.evaluate.assert_awaited_once()
            capture_context.assert_not_awaited()

    async def test_stalled_overlay_restore_cannot_hold_up_completed_action(self):
        async def stalled():
            await asyncio.Event().wait()
        page = SimpleNamespace(evaluate=Mock(side_effect=[asyncio.sleep(0), stalled()]))
        action = AsyncMock(return_value='complete')
        result = await asyncio.wait_for(
            AgentOrchestrator._with_overlay_hidden(self.orch, page, action), timeout=3)
        self.assertEqual(result, 'complete')

    async def test_no_image_task_starts_execution_with_editor_read(self):
        task = CodingTask()
        task.description = 'Build an HTML form from these written requirements.'
        orch = SimpleNamespace(
            state=SimpleNamespace(model_thinking=None), control_flags={},
            browser_mgr=SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(is_cdp_connected=True))),
            _get_llm_instance=Mock(return_value=object()), _build_controller=Mock(),
            _is_frontend_task=Mock(return_value=True), _extract_ref_images_to_disk=AsyncMock(return_value=[]),
            _log=Mock(), url='https://example.test/task', use_vision=False,
            _on_agent_step=AsyncMock(), _get_current_page_safe=AsyncMock(side_effect=[object(), None]),
            _extract_problem_from_dom_direct=AsyncMock(return_value=(task.description, [], [])),
        )
        async def run(**kwargs):
            orch._task_completed_successfully = True
        agent = SimpleNamespace(run=AsyncMock(side_effect=run))
        with patch('task_subagent.Agent', return_value=agent) as factory:
            result = await TaskSubagent(task, orch).execute()
        self.assertTrue(result.success)
        self.assertEqual(factory.call_args.kwargs['initial_actions'], [{'read_code_from_editor': {}}])
        self.assertFalse(factory.call_args.kwargs['directly_open_url'])
        self.assertIn(task.description, factory.call_args.kwargs['task'])
        agent.run.assert_awaited_once()


class TestOptionalReferencePrompts(unittest.TestCase):
    def test_execution_does_not_require_a_mockup(self):
        prompt = get_execution_system_prompt('Build a form in HTML and CSS', 1, False)
        self.assertIn('reference images are optional', prompt)
        self.assertIn('do not search again', prompt)
        self.assertNotIn('ALWAYS call `extract_and_view_reference_images`', prompt)

    def test_recon_can_complete_without_images(self):
        prompt = get_recon_task_prompt('https://example.test', False)
        self.assertIn('If no image is available, complete reconnaissance from the written brief', prompt)
