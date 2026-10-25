"""Exercise the production action without requiring browser-use or an LLM."""
import ast
import asyncio
import hashlib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from models import CodingTask, SubmissionRecord

class SubmissionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setup_action(self, record=None):
        self.task = CodingTask()
        self.adapter = SimpleNamespace(parse_result=AsyncMock(return_value=record or SubmissionRecord(terminal=False)))
        self.orch = SimpleNamespace(control_flags={}, _current_task=self.task,
            _submission_started_task_id=self.task.task_id,
            browser_mgr=SimpleNamespace(get_browser=AsyncMock(return_value=object())),
            _get_current_page_safe=AsyncMock(return_value=object()), _log=Mock())
        tree = ast.parse(Path('controller_actions.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'verify_submission_status')
        fn.decorator_list = []
        async def bounded(awaitable, timeout):
            return await asyncio.wait_for(awaitable, min(timeout, .02))
        env = dict(orchestrator=self.orch, time=time, hashlib=hashlib,
            asyncio=SimpleNamespace(wait_for=bounded, CancelledError=asyncio.CancelledError, TimeoutError=asyncio.TimeoutError),
            consume_suggestions_block=lambda *a:'', _get_sym=lambda name,fallback:fallback,
            detect_platform=AsyncMock(return_value=self.adapter))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'controller_actions.py', 'exec'),env)
        return env['verify_submission_status']

    async def test_hung_read_returns_without_navigation(self):
        action = self.setup_action()
        async def hang(*a):
            await asyncio.Event().wait()
        self.adapter.parse_result.side_effect = hang
        result = await action()
        self.assertIn('timed out',result)
        self.assertIn('Do not reload',result)

    async def test_expired_window_does_not_touch_browser(self):
        action = self.setup_action()
        self.orch._submission_check_window = (self.task.task_id, time.monotonic()-61)
        result = await action()
        self.assertIn('deadline reached',result)
        self.orch.browser_mgr.get_browser.assert_not_awaited()

    async def test_unknown_result_does_not_claim_tests_running(self):
        action = self.setup_action()
        result = await action()
        self.assertIn('No final submission verdict',result)
        self.assertIn('do not reload or resubmit',result)
