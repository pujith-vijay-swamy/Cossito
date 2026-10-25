"""Startup and cross-thread control regressions without a browser or LLM."""
import asyncio
import ast
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from human_loop import HumanInTheLoopController
from url_security import normalize_target_url

spec = importlib.util.spec_from_file_location('_browser_manager_test', Path('browser_manager.py'))
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'browser_use': SimpleNamespace(Browser=Mock())}):
    spec.loader.exec_module(module)
BrowserManager = module.BrowserManager

class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(user_data_dir=self.temp.name)
        BrowserManager._active_instance = None
        self.page = SimpleNamespace(url='about:blank')
        self.page.get_url = AsyncMock(side_effect=lambda:self.page.url)
        async def goto(url): self.page.url = url
        self.page.goto = AsyncMock(side_effect=goto)
        self.browser = SimpleNamespace(is_cdp_connected=False, start=AsyncMock(),
            get_current_page=AsyncMock(return_value=self.page))
        self.manager._build_browser = Mock(return_value=self.browser)

    def tearDown(self):
        BrowserManager._active_instance = None
        self.temp.cleanup()

    async def test_initial_navigation_and_exact_url_reuse(self):
        await self.manager.open_target_page('http://localhost:3000/task?a=1#code')
        self.browser.is_cdp_connected = True
        await self.manager.open_target_page('http://localhost:3000/task?a=1#code')
        self.page.goto.assert_awaited_once()
        self.browser.start.assert_awaited_once()

    async def test_similar_prefix_and_different_hash_require_navigation(self):
        for current in ['https://example.com/task-other', 'https://example.com/task#other']:
            self.page.url = current
            await self.manager.open_target_page('https://example.com/task#code')
        self.assertEqual(self.page.goto.await_count,2)

    async def test_start_failure_propagates_before_navigation(self):
        self.browser.start.side_effect = RuntimeError('startup failed')
        with self.assertRaisesRegex(RuntimeError,'startup failed'):
            await self.manager.open_target_page('https://example.com')
        self.page.goto.assert_not_awaited()

    async def test_navigation_failure_propagates(self):
        self.page.goto.side_effect = RuntimeError('Navigation failed')
        with self.assertRaisesRegex(RuntimeError,'Navigation failed'):
            await self.manager.open_target_page('https://example.com')

    async def test_navigation_waits_for_document_commit(self):
        async def delayed_navigation(url):
            asyncio.get_running_loop().call_later(.01, setattr, self.page, 'url', url)
        self.page.goto.side_effect = delayed_navigation
        await self.manager.open_target_page('https://example.com')
        self.assertEqual(self.page.url, 'https://example.com')

    async def test_profile_locks_preserved_and_shared_instance(self):
        lock = Path(self.temp.name)/'DevToolsActivePort'
        lock.write_text('live browser')
        browsers = await asyncio.gather(*(self.manager.get_browser() for _ in range(5)))
        self.assertTrue(all(b is self.browser for b in browsers))
        self.manager._build_browser.assert_called_once()
        self.assertTrue(lock.exists())

    async def test_resume_from_ui_thread_wakes_agent(self):
        hitl = HumanInTheLoopController()
        pause = asyncio.create_task(hitl.trigger_pause('test'))
        await asyncio.sleep(0)
        await asyncio.to_thread(hitl.resume)
        await asyncio.wait_for(pause,.5)
        self.assertFalse(hitl.is_paused)

class URLTests(unittest.TestCase):
    def test_agents_disable_library_automatic_url_extraction(self):
        # An empty initial_actions list is falsy and still enables URL extraction
        # in browser-use; verify the actual disabling option at each call site.
        for filename in ('recon_engine.py', 'task_subagent.py'):
            tree = ast.parse(Path(filename).read_text(encoding='utf-8'))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and any(k.arg == 'initial_actions' for k in n.keywords)]
            self.assertTrue(calls)
            for call in calls:
                value = next(k.value for k in call.keywords if k.arg == 'directly_open_url')
                self.assertIs(ast.literal_eval(value), False)

    def test_local_and_remote_defaults(self):
        self.assertEqual(normalize_target_url('localhost:3000'),'http://localhost:3000')
        self.assertEqual(normalize_target_url('127.0.0.1:8000'),'http://127.0.0.1:8000')
        self.assertEqual(normalize_target_url('example.com/a?q=1#x'),'https://example.com/a?q=1#x')
        self.assertEqual(normalize_target_url(' https://localhost:3000 '),'https://localhost:3000')

    def test_invalid_input_rejected(self):
        for url in ['', 'file:///tmp/index.html','http://','http://host:bad','https://user:secret@host']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_target_url(url)
