import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from editor_adapters import safe_write
from models import WorkspaceSnapshot
from verification import parse_test_results, is_submission_successful


class PreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_code_does_not_trigger_editor_change(self):
        adapter = SimpleNamespace(detect=AsyncMock(return_value=True),
            snapshot=AsyncMock(return_value=WorkspaceSnapshot(content='solution')),
            readback=AsyncMock(return_value='solution'), write=AsyncMock())
        success, _, _ = await safe_write(object(), 'solution', adapter)
        self.assertTrue(success)
        adapter.write.assert_not_awaited()

    async def test_visible_failure_is_terminal_not_pending(self):
        record = parse_test_results('Test cases failed. Please try again')
        self.assertTrue(record.terminal)
        self.assertEqual(record.outcome, 'wrong_answer')
        self.assertFalse(is_submission_successful(record))
