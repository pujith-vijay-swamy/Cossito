"""
Unit tests for platform_adapters.py.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from models import PlatformTask, SubmissionRecord
from platform_adapters import GenericAdapter, detect_platform, wait_for_terminal


class TestPlatformAdapters(unittest.TestCase):
    def test_generic_adapter_terminal_detection(self):
        adapter = GenericAdapter()
        page = MagicMock()

        # 1. Non-terminal: contains "evaluating"
        page.evaluate = AsyncMock(return_value="Evaluating test cases... Please wait.")
        loop = asyncio.new_event_loop()
        try:
            is_term = loop.run_until_complete(adapter.is_terminal(page))
            self.assertFalse(is_term)

            # 2. Terminal: contains results
            page.evaluate = AsyncMock(return_value="3/3 test cases passed. Status: Accepted")
            is_term2 = loop.run_until_complete(adapter.is_terminal(page))
            self.assertTrue(is_term2)

            # 3. Parse result
            rec = loop.run_until_complete(adapter.parse_result(page))
            self.assertTrue(rec.terminal)
            self.assertEqual(rec.passed_count, 3)
            self.assertEqual(rec.total_count, 3)
        finally:
            loop.close()

    def test_wait_for_terminal_timeout(self):
        adapter = GenericAdapter()
        page = MagicMock()
        # Always evaluating
        page.evaluate = AsyncMock(return_value="Evaluating test cases...")

        loop = asyncio.new_event_loop()
        try:
            rec = loop.run_until_complete(wait_for_terminal(page, adapter, timeout=0.05, poll_interval=0.01))
            self.assertFalse(rec.terminal)
        finally:
            loop.close()


class TestSubmissionPolling(unittest.IsolatedAsyncioTestCase):
    async def test_unrecognized_output_is_not_a_failed_terminal_verdict(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value='Waiting for results')
        result = await GenericAdapter().parse_result(page)
        self.assertFalse(result.terminal)

    async def test_completed_result_returns_without_extra_terminal_read(self):
        adapter = MagicMock()
        adapter.parse_result = AsyncMock(return_value=SubmissionRecord(terminal=True, outcome='accepted'))
        adapter.is_terminal = AsyncMock()
        result = await wait_for_terminal(MagicMock(), adapter, timeout=.1, poll_interval=.01)
        self.assertTrue(result.terminal)
        adapter.parse_result.assert_awaited_once()
        adapter.is_terminal.assert_not_awaited()

    async def test_hung_browser_read_is_bounded_and_not_retried_after_timeout(self):
        adapter = MagicMock()
        async def stalled(page):
            await asyncio.Event().wait()
        adapter.parse_result = AsyncMock(side_effect=stalled)
        result = await asyncio.wait_for(wait_for_terminal(MagicMock(), adapter, timeout=.05), timeout=.5)
        self.assertFalse(result.terminal)
        adapter.parse_result.assert_awaited_once()

    async def test_partial_results_while_busy_are_not_terminal(self):
        adapter = MagicMock()
        adapter.parse_result = AsyncMock(return_value=SubmissionRecord(terminal=False, passed_count=2, total_count=5))
        result = await wait_for_terminal(MagicMock(), adapter, timeout=.04, poll_interval=.01)
        self.assertFalse(result.terminal)

    async def test_user_cancellation_propagates(self):
        adapter = MagicMock()
        adapter.parse_result = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_terminal(MagicMock(), adapter, timeout=.1)


if __name__ == "__main__":
    unittest.main()
