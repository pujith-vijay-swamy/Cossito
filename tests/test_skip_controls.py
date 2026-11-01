import asyncio
import unittest
from types import SimpleNamespace
from human_loop import HumanInTheLoopController, consume_skip_request, is_skip_directive

class SkipControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_wakes_pause_and_is_consumed_once(self):
        hitl=HumanInTheLoopController()
        paused=asyncio.create_task(hitl.trigger_pause('Unverified'))
        await asyncio.sleep(0)
        hitl.skip()
        await asyncio.wait_for(paused,.5)
        hitl.resume()
        orch=SimpleNamespace(hitl=hitl,control_flags={'suggestions':['skip this task','Keep formatting']})
        self.assertTrue(consume_skip_request(orch))
        self.assertFalse(consume_skip_request(orch))
        self.assertEqual(orch.control_flags['suggestions'],['Keep formatting'])

    def test_coding_guidance_is_not_a_skip(self):
        for text in ('do not skip this task', 'skip whitespace in the input', 'done writing the function', 'ignore the old CSS', 'completed the header'):
            self.assertFalse(is_skip_directive(text),text)
        for text in ('skip', 'please skip this problem','skip the same problem','go to next lesson','next task'):
            self.assertTrue(is_skip_directive(text),text)
