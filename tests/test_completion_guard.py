import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from controller_actions import build_controller


class CompletionGuardTests(unittest.IsolatedAsyncioTestCase):
    def action(self, **overrides):
        orch = SimpleNamespace(_current_task=object(), _code_written_in_current_task=False,
                               _task_completed_successfully=False, _log=Mock())
        for key, value in overrides.items():
            setattr(orch, key, value)
        return build_controller(orch).registry.registry.actions['done'].function

    async def test_premature_done_returns_actionable_feedback_without_ending_run(self):
        result = await self.action()(text='Here is my plan', success=True)
        self.assertFalse(result.is_done)
        self.assertIsNone(result.error)
        self.assertIn('write_code_to_editor', result.extracted_content)

    async def test_written_code_still_requires_verification(self):
        result = await self.action(_code_written_in_current_task=True)(text='Code written')
        self.assertFalse(result.is_done)
        self.assertIn('verify_submission_status', result.extracted_content)

    async def test_verified_task_can_finish(self):
        result = await self.action(_task_completed_successfully=True)(text='Verified')
        self.assertTrue(result.is_done)
        self.assertTrue(result.success)

    async def test_recon_output_is_not_blocked(self):
        result = await self.action(_current_task=None)(text='{"tasks": []}')
        self.assertTrue(result.is_done)

    async def test_genuine_failure_preserves_explanation(self):
        result = await self.action()(text='The editor is read-only', success=False)
        self.assertTrue(result.is_done)
        self.assertFalse(result.success)
        self.assertEqual(result.extracted_content, 'The editor is read-only')

    async def test_repeated_premature_completion_is_bounded(self):
        action = self.action()
        for _ in range(2):
            self.assertFalse((await action(text='Finished')).is_done)
        result = await action(text='Finished again')
        self.assertTrue(result.is_done)
        self.assertFalse(result.success)
        self.assertIn('Finished again', result.extracted_content)
