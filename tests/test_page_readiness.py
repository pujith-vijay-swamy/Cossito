import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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

    async def test_verified_completion_dialog_is_finalized_before_readiness(self):
        blocked = {'text': '', 'busy': False, 'dialog': 'Horray!!! You have passed all the test cases. Would you like to mark this block as completed?'}
        ready = {'text': 'Change the width property to 80%.', 'busy': False}
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=[blocked, ready, ready]))
        with patch('platform_adapters.finish_verified_completion', new=AsyncMock(return_value=['marked'])) as finish:
            await wait_for_page_ready(page, timeout=.5, poll_interval=.001)
            finish.assert_awaited_once_with(page, verified=True, advance=False)

    async def test_unverified_dialog_never_clicks_or_unblocks_writing(self):
        for text in ('Sign in to continue', '2 tests passed, 1 test failed'):
            page = SimpleNamespace(evaluate=AsyncMock(return_value={'text':'Lesson text', 'busy':False, 'dialog':text}))
            with patch('platform_adapters.finish_verified_completion', new=AsyncMock()) as finish:
                with self.assertRaisesRegex(RuntimeError, 'dialog is blocking'):
                    await wait_for_page_ready(page, timeout=.1, poll_interval=.001)
                finish.assert_not_awaited()

    async def test_completion_that_wont_close_is_not_clicked_repeatedly(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={'text':'', 'busy':False, 'dialog':'You have passed all the test cases.'}))
        with patch('platform_adapters.finish_verified_completion', new=AsyncMock(return_value=[])) as finish:
            with self.assertRaisesRegex(RuntimeError, 'completion dialog'):
                await wait_for_page_ready(page, timeout=.03, poll_interval=.001)
            finish.assert_awaited_once()

    async def test_disconnect_reacquires_page_and_restarts_stability_check(self):
        ready = {'text':'Change the width to 80%.', 'busy':False}
        stale = SimpleNamespace(evaluate=AsyncMock(side_effect=[ready, ConnectionError('Client is stopping')]))
        fresh = SimpleNamespace(evaluate=AsyncMock(return_value=ready))
        recover = AsyncMock(return_value=fresh)
        result = await wait_for_page_ready(stale, timeout=.5, poll_interval=.001, recover_page=recover)
        self.assertIs(result, fresh)
        self.assertEqual(fresh.evaluate.await_count, 2)
        recover.assert_awaited_once_with(stale)

    async def test_repeated_disconnect_is_bounded(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=ConnectionError('Client is stopping')))
        recover = AsyncMock(return_value=page)
        with self.assertRaisesRegex(ConnectionError, 'has not been rewritten'):
            await wait_for_page_ready(page, timeout=.5, poll_interval=.001, recover_page=recover)
        self.assertEqual(recover.await_count, 2)

    async def test_stop_during_recovery_propagates(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=ConnectionError('Client is stopping')))
        recover = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_page_ready(page, timeout=.5, recover_page=recover)
