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


if __name__ == "__main__":
    unittest.main()
