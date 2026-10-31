import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from platform_adapters import finish_verified_completion


class PostCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_result_never_clicks(self):
        page = SimpleNamespace(evaluate=AsyncMock())
        self.assertEqual(await finish_verified_completion(page, verified=False, advance=True), [])
        page.evaluate.assert_not_awaited()

    async def test_mark_then_advance_once(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=['marked', 'waiting', 'advanced']))
        with patch('platform_adapters.asyncio.sleep', new=AsyncMock()):
            actions = await finish_verified_completion(page, verified=True, advance=True)
        self.assertEqual(actions, ['marked', 'advanced'])
        self.assertFalse(page.evaluate.await_args_list[0].args[1]['marked'])
        self.assertTrue(page.evaluate.await_args_list[1].args[1]['marked'])
        self.assertEqual(page.evaluate.await_count, 3)

    async def test_last_task_does_not_advance(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value='closed'))
        await finish_verified_completion(page, verified=True, advance=False)
        self.assertFalse(page.evaluate.await_args.args[1]['advance'])
