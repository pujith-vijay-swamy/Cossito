"""
Unit tests for overlay panel structure and lifecycle integration.
"""

import asyncio
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_core import AgentOrchestrator
from models import AgentPhase, CodingTask, PlatformTask, TaskState, TaskStatus


class TestOverlay(unittest.TestCase):
    def test_overlay_panel_html_structure(self):
        panel_path = Path("D:/auto/overlay_panel.html")
        self.assertTrue(panel_path.exists(), "overlay_panel.html must exist")
        content = panel_path.read_text(encoding="utf-8")

        # Required DOM elements
        self.assertIn('id="__omnitasker_panel"', content)
        self.assertIn('id="__omnitasker_state"', content)
        self.assertIn('id="__omnitasker_commands"', content)
        self.assertIn('id="__ot_titlebar"', content)
        self.assertIn('id="__ot_body"', content)
        self.assertIn('id="__ot_collapse_btn"', content)
        self.assertIn('id="__ot_phase_pill"', content)
        self.assertIn('id="__ot_pause_btn"', content)
        self.assertIn('id="__ot_stop_btn"', content)

        # Inline style fallback for CSP
        self.assertIn('style="position:fixed !important;', content)

        # JS IIFE separator
        self.assertIn("<!-- __OMNITASKER_JS__ -->", content)

    def test_overlay_safe_sync_overlay_method_exists(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.is_open = False

        # Must not raise AttributeError
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._safe_sync_overlay())
        finally:
            loop.close()

    def test_inject_overlay_skips_internal_pages(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._injecting_pages = set()

        page = MagicMock()
        page.get_url = AsyncMock(return_value="chrome://newtab/")
        page.evaluate = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._inject_overlay(page))
            # page.evaluate should NOT be called on chrome://newtab/
            page.evaluate.assert_not_called()
        finally:
            loop.close()

    def test_with_overlay_hidden_restores_display(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        page = MagicMock()
        eval_scripts = []

        async def mock_eval(script):
            eval_scripts.append(script)
            return True

        page.evaluate = AsyncMock(side_effect=mock_eval)

        async def sample_action():
            return "action_completed"

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(orchestrator._with_overlay_hidden(page, sample_action))
            self.assertEqual(res, "action_completed")
            self.assertEqual(len(eval_scripts), 2)
            self.assertIn("display', 'none'", eval_scripts[0])
            self.assertIn("removeProperty('display')", eval_scripts[1])
        finally:
            loop.close()

    def test_inject_overlay_trusted_types_and_cdp_js_execution(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._injecting_pages = set()
        orchestrator.state = MagicMock()
        orchestrator.state.phase = AgentPhase.RECON
        orchestrator.state.progress_pct = 0.0
        orchestrator.state.current_task_index = 0
        orchestrator.state.task_queue = None
        orchestrator.state.logs = []
        orchestrator.state.model_thinking = None
        orchestrator.state.model_next_goal = None
        orchestrator.state.model_memory = None
        orchestrator.state.model_last_actions = []
        orchestrator.state.hitl_reason = None
        orchestrator.state.failed_injection_code = None
        orchestrator._log = MagicMock()

        page = MagicMock()
        page.get_url = AsyncMock(return_value="https://leetcode.com/problems/two-sum/")
        eval_calls = []

        async def mock_eval(script, *args):
            eval_calls.append((script, args))
            # First call: check if panel exists and js is ready
            if "panel: !!document.getElementById" in script:
                return {"panel": False, "js": False}
            # Anti-debugger script
            if "const _Function = window.Function" in script:
                return None
            # Atomic injection script
            if "atomic_injection_script" in script or "document.adoptedStyleSheets" in script:
                self.assertIn("window.trustedTypes", script)
                self.assertIn("adoptedStyleSheets", script)
                self.assertIn("DOMParser", script)
                return True
            # Direct CDP JS evaluation
            if "'use strict';" in script and "__omnitasker_js_initialized" in script:
                return None
            # State sync
            if "__omnitasker_state" in script:
                return None
            return True

        page.evaluate = AsyncMock(side_effect=mock_eval)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._inject_overlay(page))
            # Verify direct JS execution was evaluated via CDP
            js_eval_called = any(
                "'use strict';" in call[0] and "__omnitasker_js_initialized" in call[0]
                for call in eval_calls
            )
            self.assertTrue(js_eval_called, "Overlay JS must be evaluated directly via CDP")
        finally:
            loop.close()

    def test_sync_overlay_state_ensures_display_flex(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.state = MagicMock()
        orchestrator.state.phase = AgentPhase.EXECUTING
        orchestrator.state.progress_pct = 0.5
        orchestrator.state.current_task_index = 1
        orchestrator.state.task_queue = None
        orchestrator.state.logs = []
        orchestrator.state.model_thinking = None
        orchestrator.state.model_next_goal = None
        orchestrator.state.model_memory = None
        orchestrator.state.model_last_actions = []
        orchestrator.state.hitl_reason = None
        orchestrator.state.failed_injection_code = None

        page = MagicMock()
        eval_scripts = []

        async def mock_eval(script, *args):
            eval_scripts.append(script)
            return True

        page.evaluate = AsyncMock(side_effect=mock_eval)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._sync_overlay_state(page))
            self.assertTrue(len(eval_scripts) > 0)
            sync_script = eval_scripts[0]
            self.assertIn("display', 'flex', 'important'", sync_script)
            self.assertIn("mountTarget.appendChild(panel)", sync_script)
        finally:
            loop.close()

    def test_agent_orchestrator_init_has_injecting_pages(self):
        orchestrator = AgentOrchestrator(
            url="https://mentorpick.com/coursev2/test/chapter/123",
            api_key="test-api-key",
        )
        self.assertTrue(hasattr(orchestrator, "_injecting_pages"))
        self.assertIsInstance(orchestrator._injecting_pages, set)
        self.assertEqual(len(orchestrator._injecting_pages), 0)

    def test_inject_overlay_defensive_injecting_pages_attribute(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        # Verify _injecting_pages does not exist initially
        self.assertFalse(hasattr(orchestrator, "_injecting_pages"))

        page = MagicMock()
        page.get_url = AsyncMock(return_value="chrome://newtab/")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._inject_overlay(page))
            self.assertTrue(hasattr(orchestrator, "_injecting_pages"))
            self.assertIsInstance(orchestrator._injecting_pages, set)
        finally:
            loop.close()

    def test_navigate_to_task_question_matches_by_title(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._log = MagicMock()
        browser = MagicMock()
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        eval_args = []

        async def mock_eval(script, args=None):
            eval_args.append((script, args))
            return "Clicked question tab matching title 'Print 1 to N - without for loop'"

        page.evaluate = AsyncMock(side_effect=mock_eval)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        task = MagicMock()
        task.queue_index = 0
        task.platform_task = MagicMock()
        task.platform_task.key = "1"
        task.platform_task.title = "Print 1 to N - without for loop"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._navigate_to_task_question(task, total_tasks=3))
            self.assertEqual(len(eval_args), 1)
            script, passed_args = eval_args[0]
            self.assertEqual(passed_args.get("taskKey"), "1")
            self.assertEqual(passed_args.get("taskTitle"), "Print 1 to N - without for loop")
            self.assertIn("args.taskTitle", script)
            self.assertIn("allLinks", script)
        finally:
            loop.close()

    def test_extract_problem_from_dom_direct_includes_mantine_selectors(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        page = MagicMock()
        eval_scripts = []

        async def mock_eval(script, *args):
            eval_scripts.append(script)
            if "cleanText" in script:
                return {"text": "Given an integer N, print all numbers from 1 to N without using any for loop.", "images": []}
            if "expand_script" in script or "expandedAny" in script:
                return True
            if "candidateElems" in script:
                return [{"text": "Print 1 to N - without for loop", "completed": False}]
            return None

        page.evaluate = AsyncMock(side_effect=mock_eval)

        loop = asyncio.new_event_loop()
        try:
            text, questions, images = loop.run_until_complete(orchestrator._extract_problem_from_dom_direct(page))
            self.assertIn("Given an integer N", text)
            self.assertEqual(len(questions), 1)
            self.assertEqual(questions[0]["text"], "Print 1 to N - without for loop")

            # Check that DOM extraction script has Mantine typography selectors & scrollable exclusions
            dom_script = eval_scripts[0]
            self.assertIn(".mantine-AppShell-main .mantine-TypographyStylesProvider-root", dom_script)
            self.assertIn("main .mantine-TypographyStylesProvider-root", dom_script)
            self.assertIn('[class*="TypographyStylesProvider"]', dom_script)
            self.assertIn("elem.closest('nav, header, footer, [class*=\"Navbar\"], [class*=\"sidebar\"], [class*=\"nav\"]')", dom_script)

            # Check that question nav scripts include Mantine NavLink and accordion expansion
            nav_scripts = " ".join(eval_scripts[1:])
            self.assertIn(".mantine-NavLink-root", nav_scripts)
            self.assertIn('button[class*="NavLink"]', nav_scripts)
            self.assertIn("practice", nav_scripts)
        finally:
            loop.close()

    def test_fast_recon_mentorpick_chapter_autoclick(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.url = "https://mentorpick.com/coursev2/abc/chapter/123"
        orchestrator._log = MagicMock()
        orchestrator._cleanup_ref_images = MagicMock()
        orchestrator._background_poll_loop = AsyncMock()
        orchestrator._hitl_gate = AsyncMock()
        orchestrator._disable_cdp_debugger_pauses = AsyncMock()
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        browser = MagicMock()
        browser.is_cdp_connected = True
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        url_state = {"url": "https://mentorpick.com/coursev2/abc/chapter/123"}

        async def mock_get_url():
            return url_state["url"]

        page.get_url = AsyncMock(side_effect=mock_get_url)
        page.goto = AsyncMock()

        eval_calls = []

        async def mock_eval(script, *args):
            eval_calls.append((script, args))
            if "Clicked question" in script or "fallbackLink" in script:
                url_state["url"] = "https://mentorpick.com/coursev2/abc/chapter/123?active=q1"
                return "Clicked question matching title: Print 1 to N - without for loop"
            return None

        page.evaluate = AsyncMock(side_effect=mock_eval)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        extract_call_count = [0]

        async def mock_extract(p, user_selector=None):
            extract_call_count[0] += 1
            if extract_call_count[0] == 1:
                return "", [
                    {"text": "Print 1 to N - without for loop", "completed": False},
                    {"text": "Sum of Digits", "completed": False},
                ], []
            else:
                return "Given an integer N, print all numbers from 1 to N without using any for loop. Constraints: 1 <= N <= 1000.", [
                    {"text": "Print 1 to N - without for loop", "completed": False},
                    {"text": "Sum of Digits", "completed": False},
                ], []

        orchestrator._extract_problem_from_dom_direct = AsyncMock(side_effect=mock_extract)

        loop = asyncio.new_event_loop()
        try:
            tq = loop.run_until_complete(orchestrator._run_recon())
            self.assertIsNotNone(tq)
            self.assertEqual(len(tq.tasks), 2)
            self.assertEqual(tq.tasks[0].platform_task.title, "Print 1 to N - without for loop")
            self.assertIn("Given an integer N", tq.tasks[0].description)
            self.assertEqual(tq.tasks[1].platform_task.title, "Sum of Digits")
            self.assertIn("Solve the coding problem for Sum of Digits", tq.tasks[1].description)
            self.assertEqual(extract_call_count[0], 2)
            self.assertIn("?active=q1", tq.source_url)
        finally:
            loop.close()

    def test_fast_recon_single_question_creates_task_queue(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.url = "https://mentorpick.com/coursev2/abc/chapter/123"
        orchestrator._log = MagicMock()
        orchestrator._cleanup_ref_images = MagicMock()
        orchestrator._background_poll_loop = AsyncMock()
        orchestrator._hitl_gate = AsyncMock()
        orchestrator._disable_cdp_debugger_pauses = AsyncMock()
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        browser = MagicMock()
        browser.is_cdp_connected = True
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        page.get_url = AsyncMock(return_value="https://mentorpick.com/coursev2/abc/chapter/123?active=q1")
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        async def mock_extract(p, user_selector=None):
            return "Sample single problem description", [
                {"text": "Only Problem", "completed": False}
            ], []

        orchestrator._extract_problem_from_dom_direct = AsyncMock(side_effect=mock_extract)

        loop = asyncio.new_event_loop()
        try:
            tq = loop.run_until_complete(orchestrator._run_recon())
            self.assertIsNotNone(tq)
            self.assertEqual(len(tq.tasks), 1)
            self.assertEqual(tq.tasks[0].platform_task.title, "Only Problem")
            self.assertIn("Sample single problem description", tq.tasks[0].description)
        finally:
            loop.close()

    def test_expand_script_protects_child_navlinks(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        page = MagicMock()
        eval_scripts = []

        async def mock_eval(script, *args):
            eval_scripts.append(script)
            if "expand_script" in script or "expandedAny" in script:
                return False
            return []

        page.evaluate = AsyncMock(side_effect=mock_eval)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._extract_problem_from_dom_direct(page))
            self.assertTrue(len(eval_scripts) >= 2)
            expand_js = eval_scripts[1]
            self.assertIn(".mantine-NavLink-children", expand_js)
            self.assertIn("isAccordionControl", expand_js)
            self.assertIn("data-opened", expand_js)
        finally:
            loop.close()

    def test_section_expansion_and_multi_problem_cataloguing(self):
        """Verify that multi-problem section is catalogued into individual CodingTasks and preserves completed status."""
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.url = "https://mentorpick.com/coursev2/test/chapter/ch1"
        orchestrator._log = MagicMock()
        orchestrator._hitl_gate = AsyncMock()
        orchestrator._disable_cdp_debugger_pauses = AsyncMock()
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        browser = MagicMock()
        browser.is_cdp_connected = True
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        page.get_url = AsyncMock(return_value=orchestrator.url)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        # Mock direct DOM extraction returning 3 child questions (one already completed)
        section_questions = [
            {"text": "1. Two Sum", "completed": True},
            {"text": "2. Reverse Array", "completed": False},
            {"text": "3. Valid Parentheses", "completed": False},
        ]
        orchestrator._extract_problem_from_dom_direct = AsyncMock(
            return_value=("Practice Problem Statement", section_questions, [])
        )

        loop = asyncio.new_event_loop()
        try:
            tq = loop.run_until_complete(orchestrator._run_recon())
            self.assertIsNotNone(tq)
            self.assertEqual(len(tq.tasks), 3)
            # Task 1 is completed
            self.assertEqual(tq.tasks[0].platform_task.title, "1. Two Sum")
            self.assertEqual(tq.tasks[0].status, TaskStatus.SUCCESS)
            self.assertEqual(tq.tasks[0].state, TaskState.PASSED)
            # Task 2 is pending
            self.assertEqual(tq.tasks[1].platform_task.title, "2. Reverse Array")
            self.assertEqual(tq.tasks[1].status, TaskStatus.PENDING)
            # Task 3 is pending
            self.assertEqual(tq.tasks[2].platform_task.title, "3. Valid Parentheses")
            self.assertEqual(tq.tasks[2].status, TaskStatus.PENDING)
        finally:
            loop.close()

    def test_navigation_script_matches_numbered_keys(self):
        """Verify navigation regex patterns match numbered question tabs like '2. Reverse Array'."""
        key_patterns = [
            re.compile(r'^(?:question|task|problem|q)?\s*2[\.:\)\s]', re.IGNORECASE),
            re.compile(r'^(?:question|task|problem|q)\s*2$', re.IGNORECASE),
            re.compile(r'^2$')
        ]
        candidates = ["2. Reverse Array", "2: Reverse Array", "2) Reverse Array", "Question 2", "Problem 2", "2"]
        for c in candidates:
            self.assertTrue(any(p.search(c) for p in key_patterns), f"Pattern did not match candidate: {c}")

        # Should not match other numbers
        self.assertFalse(any(p.search("3. Reverse Array") for p in key_patterns))
        self.assertFalse(any(p.search("Question 3") for p in key_patterns))

    def test_consume_suggestions_block_formats_and_clears(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.control_flags = {"suggestions": ["Use O(n) hash map", "Handle empty array case"]}
        orchestrator._log = MagicMock()

        block = orchestrator._consume_suggestions_block("test_action")
        self.assertIn("ACTIVE HUMAN USER DIRECTIVES", block)
        self.assertIn("Use O(n) hash map", block)
        self.assertIn("Handle empty array case", block)
        self.assertEqual(orchestrator.control_flags["suggestions"], [])

        # Second call returns empty string
        second_block = orchestrator._consume_suggestions_block("test_action")
        self.assertEqual(second_block, "")

    def test_inject_overlay_discards_injecting_pages_guard(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._injecting_pages = set()
        page = MagicMock()
        page._target_id = "page-target-xyz"
        page.get_url = AsyncMock(return_value="https://leetcode.com/problems/test")
        page.evaluate = AsyncMock(return_value={"panel": True, "js": True})
        orchestrator._sync_overlay_state = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._inject_overlay(page))
            # Must be discarded from _injecting_pages so future injections are allowed
            self.assertNotIn("page-target-xyz", orchestrator._injecting_pages)
        finally:
            loop.close()

    def test_extract_problem_description_action_no_nameerror(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.control_flags = {"suggestions": ["Format output as JSON"]}
        orchestrator._log = MagicMock()
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock()
        orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        controller = orchestrator._build_controller()
        action_def = getattr(controller.registry, "registry", controller.registry).actions.get("extract_problem_description")
        self.assertIsNotNone(action_def, "extract_problem_description action must exist")
        extract_action = action_def.function

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(extract_action())
            self.assertIn("Error: Active browser page is currently unavailable", res)
            self.assertIn("Format output as JSON", res)
        finally:
            loop.close()


    def test_overlay_starts_in_expanded_mode_with_rounded_square_pill(self):
        panel_path = Path("D:/auto/overlay_panel.html")
        content = panel_path.read_text(encoding="utf-8")
        # Starts expanded by default
        self.assertIn('id="__omnitasker_panel"', content)
        self.assertNotIn('id="__omnitasker_panel" class="__ot_collapsed_ball"', content)
        self.assertIn('width:380px !important;', content)
        self.assertIn('var collapsed = false;', content)
        # Collapsed pill is in a sleek rounded square / squircle style (14px border radius, not 50% circle)
        self.assertIn('border-radius: 14px !important;', content)
        self.assertIn('width: 52px !important;', content)
        self.assertIn('height: 52px !important;', content)

        # Verify phase accent rules exist for collapsed pill
        self.assertIn('.__ot_collapsed_ball.--phase-recon', content)
        self.assertIn('.__ot_collapsed_ball.--phase-exec', content)
        self.assertIn('.__ot_collapsed_ball.--phase-done', content)
        self.assertIn('.__ot_collapsed_ball.--phase-error', content)

    def test_overlay_floating_pill_lifecycle_in_browser(self):
        import asyncio
        from playwright.async_api import async_playwright

        async def run_lifecycle_test():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto("about:blank")

                panel_path = Path("D:/auto/overlay_panel.html")
                raw = panel_path.read_text(encoding="utf-8")
                parts = raw.split("<!-- __OMNITASKER_JS__ -->")
                html_markup = parts[0].strip()
                js_code = parts[1].strip() if len(parts) > 1 else ""

                # Inject markup and JS
                await page.evaluate(
                    "(html) => document.documentElement.insertAdjacentHTML('beforeend', html)",
                    html_markup,
                )
                clean_js = js_code.strip()
                if not (clean_js.startswith('(') and '=>' in clean_js):
                    clean_js = f"() => {{ {clean_js} }}"
                await page.evaluate(clean_js)

                # 1. Verify starts expanded by default as 380px panel
                w_initial = await page.evaluate("() => document.getElementById('__omnitasker_panel').offsetWidth")
                is_collapsed_init = await page.evaluate("() => document.getElementById('__omnitasker_panel').classList.contains('__ot_collapsed_ball')")
                self.assertEqual(w_initial, 380)
                self.assertFalse(is_collapsed_init)

                # Update UI while expanded to 'executing'
                await page.evaluate("() => window.__ot_updateUI({ phase: 'executing', totalTasks: 2, currentTask: 0 })")

                # 2. Click collapse button -> collapses into 52px rounded square pill with executing phase glow
                await page.evaluate("() => document.getElementById('__ot_collapse_btn').click()")
                w_collapsed = await page.evaluate("() => document.getElementById('__omnitasker_panel').offsetWidth")
                is_collapsed = await page.evaluate("() => document.getElementById('__omnitasker_panel').classList.contains('__ot_collapsed_ball')")
                self.assertEqual(w_collapsed, 52)
                self.assertTrue(is_collapsed)

                # Wait for 150ms CSS transition to complete
                await asyncio.sleep(0.25)

                # Verify rounded square pill border-radius is 14px
                pill_radius = await page.evaluate("() => window.getComputedStyle(document.getElementById('__omnitasker_panel')).borderRadius")
                self.assertEqual(pill_radius, "14px")

                # Verify dynamic executing phase glow (green border & shadow)
                border_exec = await page.evaluate("() => window.getComputedStyle(document.getElementById('__omnitasker_panel')).borderColor")
                shadow_exec = await page.evaluate("() => window.getComputedStyle(document.getElementById('__omnitasker_panel')).boxShadow")
                self.assertEqual(border_exec, "rgb(74, 222, 128)")
                self.assertIn("rgba(74, 222, 128, 0.4)", shadow_exec)

                # Transition to done phase while collapsed
                await page.evaluate("() => window.__ot_updateUI({ phase: 'done', totalTasks: 2, currentTask: 1 })")
                await asyncio.sleep(0.25)
                border_done = await page.evaluate("() => window.getComputedStyle(document.getElementById('__omnitasker_panel')).borderColor")
                shadow_done = await page.evaluate("() => window.getComputedStyle(document.getElementById('__omnitasker_panel')).boxShadow")
                self.assertEqual(border_done, "rgb(168, 85, 247)")
                self.assertIn("rgba(168, 85, 247, 0.4)", shadow_done)

                # 3. Click pill -> expands back to 380px panel
                await page.evaluate("() => document.getElementById('__omnitasker_panel').click()")
                w_expanded = await page.evaluate("() => document.getElementById('__omnitasker_panel').offsetWidth")
                is_expanded = await page.evaluate("() => !document.getElementById('__omnitasker_panel').classList.contains('__ot_collapsed_ball')")
                self.assertEqual(w_expanded, 380)
                self.assertTrue(is_expanded)

                # 4. Collapse again -> collapses to 52px rounded square pill
                await page.evaluate("() => document.getElementById('__ot_collapse_btn').click()")
                w_collapsed_again = await page.evaluate("() => document.getElementById('__omnitasker_panel').offsetWidth")
                self.assertEqual(w_collapsed_again, 52)

                # 5. Trigger HITL via updateUI -> auto-expands to 380px
                await page.evaluate("""() => {
                    if (window.__ot_updateUI) {
                        window.__ot_updateUI({
                            phase: 'paused_hitl',
                            hitlReason: 'Please enter verification code',
                            totalTasks: 1,
                            currentTask: 0,
                            logs: []
                        });
                    }
                }""")
                w_auto_expanded = await page.evaluate("() => document.getElementById('__omnitasker_panel').offsetWidth")
                hitl_visible = await page.evaluate("() => document.getElementById('__ot_hitl').classList.contains('--visible')")
                self.assertEqual(w_auto_expanded, 380)
                self.assertTrue(hitl_visible)

                await browser.close()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_lifecycle_test())
        finally:
            loop.close()

    def test_safe_sync_overlay_reinjects_when_panel_missing(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.is_open = True
        browser = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        # Panel missing in DOM
        page.evaluate = AsyncMock(return_value=False)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._safe_sync_overlay())
            orchestrator._inject_overlay.assert_called_once_with(page)
            orchestrator._sync_overlay_state.assert_not_called()
        finally:
            loop.close()

    def test_get_current_page_safe_auto_reconnects_when_cdp_disconnected(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.is_open = True

        browser = MagicMock()
        browser.is_cdp_connected = False
        reconnected = [False]

        async def mock_reconnect():
            reconnected[0] = True
            browser.is_cdp_connected = True

        browser.reconnect = AsyncMock(side_effect=mock_reconnect)
        mock_page = MagicMock()
        browser.get_current_page = AsyncMock(return_value=mock_page)

        loop = asyncio.new_event_loop()
        try:
            page = loop.run_until_complete(orchestrator._get_current_page_safe(browser))
            self.assertTrue(reconnected[0], "reconnect() should be called when is_cdp_connected is False")
            self.assertEqual(page, mock_page)
        finally:
            loop.close()

    def test_run_recon_defensive_dom_recovery_on_agent_error(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.url = "https://mentorpick.com/coursev2/abc/chapter/123"
        orchestrator._log = MagicMock()
        orchestrator._cleanup_ref_images = MagicMock()
        orchestrator._background_poll_loop = AsyncMock()
        orchestrator._hitl_gate = AsyncMock()
        orchestrator._disable_cdp_debugger_pauses = AsyncMock()
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        browser = MagicMock()
        browser.is_cdp_connected = True
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        page.get_url = AsyncMock(return_value="https://mentorpick.com/coursev2/abc/chapter/123")
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        # Direct extraction in fast recon initially returns empty so it falls through to autonomous recon
        direct_calls = [0]
        async def mock_extract(p, user_selector=None):
            direct_calls[0] += 1
            if direct_calls[0] <= 6:
                return "", [], []
            # During defensive DOM recovery after error, return recovered problem!
            return "Defensively recovered problem text: Given array nums, return indices of two numbers that add up to target.", [
                {"text": "Two Sum", "completed": False}
            ], []

        orchestrator._extract_problem_from_dom_direct = AsyncMock(side_effect=mock_extract)

        # Mock autonomous recon agent to raise a 503 UNAVAILABLE / timeout error
        mock_agent = MagicMock()
        async def mock_run(max_steps=4):
            raise Exception("503 UNAVAILABLE: The model is overloaded. Please try again later.")
        mock_agent.run = mock_run

        with patch("agent_core.Agent", return_value=mock_agent):
            with patch("agent_core.get_llm", return_value=MagicMock()):
                loop = asyncio.new_event_loop()
                try:
                    tq = loop.run_until_complete(orchestrator._run_recon())
                    self.assertIsNotNone(tq, "TaskQueue should be recovered defensively instead of crashing with 503")
                    self.assertEqual(len(tq.tasks), 1)
                    self.assertEqual(tq.tasks[0].platform_task.title, "Two Sum")
                    self.assertIn("Defensively recovered problem text", tq.tasks[0].description)
                finally:
                    loop.close()


    def test_execute_single_task_polling_re_extraction(self):
        from models import CodingTask, PlatformTask, TaskState
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._log = MagicMock()
        orchestrator._notify = MagicMock()
        orchestrator._navigate_to_task_question = AsyncMock()
        orchestrator.hitl = MagicMock()
        orchestrator.hitl.skip_requested = False
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock()

        page = MagicMock()
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        task = CodingTask(
            queue_index=1,
            platform_task=PlatformTask(key="2", title="Task 2"),
        )
        task.description = "Solve the coding problem for Task 2 in the section."

        extract_polls = [0]
        async def mock_extract(p, user_selector=None):
            extract_polls[0] += 1
            if extract_polls[0] == 1:
                return "", [], []
            return "Re-extracted complete problem statement with full markdown content and test cases.", [], []

        orchestrator._extract_problem_from_dom_direct = AsyncMock(side_effect=mock_extract)
        async def mock_solve_and_verify(t, total_tasks=1):
            t.state = TaskState.VERIFYING
            return True

        orchestrator._solve_and_verify = AsyncMock(side_effect=mock_solve_and_verify)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._execute_single_task(task, total_tasks=2))
            self.assertEqual(extract_polls[0], 2, "Should poll until description is re-extracted")
            self.assertIn("Re-extracted complete problem statement", task.description)
            self.assertEqual(task.state, TaskState.PASSED)
        finally:
            loop.close()

    def test_run_recon_prefers_browser_navigate_to_with_page_goto_fallback(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.url = "https://mentorpick.com/coursev2/test/chapter/1"
        orchestrator._log = MagicMock()
        orchestrator._hitl_gate = AsyncMock()
        orchestrator._disable_cdp_debugger_pauses = AsyncMock()
        orchestrator._inject_overlay = AsyncMock()
        orchestrator._sync_overlay_state = AsyncMock()

        browser = MagicMock()
        browser.is_cdp_connected = True
        navigated_urls = []
        async def mock_nav(url):
            navigated_urls.append(("browser.navigate_to", url))
        browser.navigate_to = AsyncMock(side_effect=mock_nav)
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        page.get_url = AsyncMock(return_value="about:blank")
        page.goto = AsyncMock()
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)
        orchestrator._extract_problem_from_dom_direct = AsyncMock(
            return_value=("Sample problem description with sufficient length for testing", [], [])
        )

        loop = asyncio.new_event_loop()
        try:
            tq = loop.run_until_complete(orchestrator._run_recon())
            self.assertIsNotNone(tq)
            self.assertEqual(len(navigated_urls), 1)
            self.assertEqual(navigated_urls[0], ("browser.navigate_to", "https://mentorpick.com/coursev2/test/chapter/1"))
            page.goto.assert_not_called()
        finally:
            loop.close()

    def test_navigate_to_task_question_accordion_expansion(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._log = MagicMock()
        browser = MagicMock()
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        eval_args = []
        async def mock_eval(script, args):
            eval_args.append((script, args))
            self.assertIn("expandParents(el)", script, "nav_script must call expandParents before clicking tab")
            return "Clicked question tab matching title 'Two Sum'"
        page.evaluate = AsyncMock(side_effect=mock_eval)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        task = CodingTask(queue_index=1, platform_task=PlatformTask(key="2", title="Two Sum"))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._navigate_to_task_question(task, total_tasks=2))
            self.assertEqual(len(eval_args), 1)
            self.assertEqual(eval_args[0][1]["taskKey"], "2")
            self.assertEqual(eval_args[0][1]["taskTitle"], "Two Sum")
        finally:
            loop.close()

    def test_execute_single_task_defensive_body_text_fallback(self):
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator._log = MagicMock()
        orchestrator._notify = MagicMock()
        orchestrator._navigate_to_task_question = AsyncMock()

        browser = MagicMock()
        orchestrator.browser_mgr = MagicMock()
        orchestrator.browser_mgr.get_browser = AsyncMock(return_value=browser)

        page = MagicMock()
        # Direct extraction returns empty
        orchestrator._extract_problem_from_dom_direct = AsyncMock(return_value=("", [], []))
        # Body evaluate returns problem text
        body_problem = "Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target. Input: nums = [2,7,11,15], target = 9. Output: [0,1]"
        page.evaluate = AsyncMock(return_value=body_problem)
        orchestrator._get_current_page_safe = AsyncMock(return_value=page)

        async def mock_solve(t, total_tasks=1):
            t.state = TaskState.PASSED
            return True
        orchestrator._solve_and_verify = AsyncMock(side_effect=mock_solve)

        task = CodingTask(queue_index=1, platform_task=PlatformTask(key="2", title="Two Sum"))
        task.description = "Two Sum. Solve the coding problem for Two Sum in the section."

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orchestrator._execute_single_task(task, total_tasks=2))
            self.assertIn("Given an array of integers nums", task.description)
            self.assertEqual(task.state, TaskState.PASSED)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()



