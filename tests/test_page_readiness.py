import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dom_extraction import wait_for_page_ready, clean_extracted_problem_text, is_loading_placeholder

class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_loading_to_be_replaced_and_content_to_stabilize(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            {'text':'Loading your workspace. Please wait while we prepare everything for you.', 'busy':False},
            {'text':'Build an HTML form', 'busy':True},
            {'text':'Build an HTML form with a submit button.', 'busy':False},
            {'text':'Build an HTML form with a submit button.', 'busy':False},
        ]))
        await wait_for_page_ready(page, timeout=.5, poll_interval=.001)
        self.assertEqual(page.evaluate.await_count,4)

    async def test_permanent_loading_fails_instead_of_becoming_a_task(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={'text':'Please wait, loading the coding challenge.', 'busy':False}))
        with self.assertRaisesRegex(RuntimeError,'refusing to solve'):
            await wait_for_page_ready(page, timeout=.02, poll_interval=.001)

    async def test_hung_dom_read_is_bounded(self):
        async def hang(*args): await asyncio.Event().wait()
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=hang))
        with self.assertRaisesRegex(RuntimeError,'refusing to solve'):
            await wait_for_page_ready(page, timeout=.02)

    async def test_cancellation_propagates(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_page_ready(page)

    def test_loading_text_is_rejected_but_task_about_loading_is_valid(self):
        self.assertEqual(clean_extracted_problem_text('Loading your workspace. Please wait while the application prepares your coding experience.'),'')
        statement = 'Build a loading screen with an animated spinner using HTML and CSS.'
        self.assertFalse(is_loading_placeholder(statement))
        self.assertEqual(clean_extracted_problem_text(statement),statement)
