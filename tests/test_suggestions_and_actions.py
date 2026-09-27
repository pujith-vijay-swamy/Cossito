"""
Unit tests for human suggestions consumption and controller actions in agent_core.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_core import AgentOrchestrator


class TestSuggestionsAndActions(unittest.TestCase):
    def setUp(self):
        self.orchestrator = AgentOrchestrator(
            url="https://example.com",
            api_key="test-api-key",
            headless=True,
        )

    def test_consume_suggestions_block_empty(self):
        # Default has empty suggestions
        block = self.orchestrator._consume_suggestions_block("test_ctx")
        self.assertEqual(block, "")

    def test_consume_suggestions_block_none_flags(self):
        self.orchestrator.control_flags = None
        block = self.orchestrator._consume_suggestions_block("test_ctx")
        self.assertEqual(block, "")

    def test_consume_suggestions_block_active(self):
        self.orchestrator.control_flags = {"suggestions": ["Directive 1", "Directive 2"]}
        block = self.orchestrator._consume_suggestions_block("test_ctx")
        self.assertIn("Directive 1", block)
        self.assertIn("Directive 2", block)
        self.assertIn("ACTIVE HUMAN USER DIRECTIVES", block)
        # Verify consumed
        self.assertEqual(self.orchestrator.control_flags["suggestions"], [])

    def test_consume_suggestions_block_inline(self):
        self.orchestrator.control_flags = {"suggestions": ["Directive 1", "Directive 2"]}
        block = self.orchestrator._consume_suggestions_block("test_ctx", inline=True)
        self.assertIn("Directive 1; Directive 2", block)
        self.assertIn("IMPORTANT: New instructions/suggestions from user:", block)
        self.assertEqual(self.orchestrator.control_flags["suggestions"], [])

    def test_extract_problem_description_no_suggestions_no_name_error(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["extract_problem_description"].function

        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)
        self.orchestrator._extract_problem_from_dom_direct = AsyncMock(
            return_value=("Sample problem statement", [], [])
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("SUCCESSFULLY EXTRACTED PROBLEM DESCRIPTION", res)
            self.assertIn("Sample problem statement", res)
        finally:
            loop.close()

    def test_extract_problem_description_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["extract_problem_description"].function

        self.orchestrator.control_flags["suggestions"] = ["Use two-pointer technique"]
        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)
        self.orchestrator._extract_problem_from_dom_direct = AsyncMock(
            return_value=("Sample problem statement", [], [])
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("SUCCESSFULLY EXTRACTED PROBLEM DESCRIPTION", res)
            self.assertIn("Use two-pointer technique", res)
            self.assertEqual(self.orchestrator.control_flags["suggestions"], [])
        finally:
            loop.close()

    def test_extract_problem_description_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["extract_problem_description"].function

        self.orchestrator.control_flags["suggestions"] = ["Do not crash on null page"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Error: Active browser page is currently unavailable", res)
            self.assertIn("Do not crash on null page", res)
            self.assertEqual(self.orchestrator.control_flags["suggestions"], [])
        finally:
            loop.close()

    def test_extract_problem_description_dom_error_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["extract_problem_description"].function

        self.orchestrator.control_flags["suggestions"] = ["Recover gracefully"]
        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)
        self.orchestrator._extract_problem_from_dom_direct = AsyncMock(
            side_effect=RuntimeError("CDP connection failed")
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Error: Failed to extract problem description", res)
            self.assertIn("Recover gracefully", res)
            self.assertEqual(self.orchestrator.control_flags["suggestions"], [])
        finally:
            loop.close()

    def test_read_code_from_editor_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["read_code_from_editor"].function

        self.orchestrator.control_flags["suggestions"] = ["Preserve comments"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Error: Active page unavailable to read code.", res)
            self.assertIn("Preserve comments", res)
        finally:
            loop.close()

    def test_write_code_to_editor_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["write_code_to_editor"].function

        self.orchestrator.control_flags["suggestions"] = ["Check edge case N=0"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(code="print('hello')"))
            self.assertIn("Error: Active page unavailable.", res)
            self.assertIn("Check edge case N=0", res)
        finally:
            loop.close()

    def test_reset_editor_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["reset_editor"].function

        self.orchestrator.control_flags["suggestions"] = ["Reset properly"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Error: Active page unavailable to reset editor.", res)
            self.assertIn("Reset properly", res)
        finally:
            loop.close()

    def test_submit_code_solution_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["submit_code_solution"].function

        self.orchestrator.control_flags["suggestions"] = ["Use fast I/O"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(code="def solve(): pass"))
            self.assertIn("Error: Active page unavailable.", res)
            self.assertIn("Use fast I/O", res)
        finally:
            loop.close()

    def test_verify_submission_status_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["verify_submission_status"].function

        self.orchestrator.control_flags["suggestions"] = ["Check test case 4"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Active page unavailable to check submission status.", res)
            self.assertIn("Check test case 4", res)
        finally:
            loop.close()

    def test_describe_page_visuals_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["describe_page_visuals"].function

        self.orchestrator.control_flags["suggestions"] = ["Examine submit button"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(query="What is on the screen?"))
            self.assertIn("Error: No active browser page available to capture screenshot.", res)
            self.assertIn("Examine submit button", res)
            self.assertEqual(self.orchestrator.control_flags["suggestions"], [])
        finally:
            loop.close()

    def test_describe_page_visuals_screenshot_error_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["describe_page_visuals"].function

        self.orchestrator.control_flags["suggestions"] = ["Check screenshot failure"]
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.screenshot = AsyncMock(side_effect=RuntimeError("CDP target detached"))
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(query="Inspect UI"))
            self.assertIn("Error: Could not take screenshot", res)
            self.assertIn("Check screenshot failure", res)
        finally:
            loop.close()

    def test_extract_and_view_reference_images_error_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["extract_and_view_reference_images"].function

        self.orchestrator.control_flags["suggestions"] = ["Extract images carefully"]
        self.orchestrator._extract_ref_images_to_disk = AsyncMock(
            side_effect=RuntimeError("Disk write failed")
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn())
            self.assertIn("Error: Failed to extract reference images", res)
            self.assertIn("Extract images carefully", res)
        finally:
            loop.close()

    def test_skip_current_task_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["skip_current_task"].function

        self.orchestrator.control_flags["suggestions"] = ["Skip to task 2"]
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(reason="Reading task"))
            self.assertIn("skipped successfully", res)
            self.assertIn("Skip to task 2", res)
        finally:
            loop.close()

    def test_pause_for_human_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["pause_for_human"].function

        async def fake_trigger_pause(reason):
            # Human injects suggestions during pause modal
            self.orchestrator.control_flags["suggestions"] = ["Logged in successfully"]

        self.orchestrator.hitl.trigger_pause = AsyncMock(side_effect=fake_trigger_pause)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(reason="Need 2FA login"))
            self.assertIn("User has logged in / solved CAPTCHA. Continuing.", res)
            self.assertIn("Logged in successfully", res)
        finally:
            loop.close()

    def test_report_task_result_without_code_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["report_task_result"].function

        self.orchestrator._code_written_in_current_task = False
        self.orchestrator.control_flags["suggestions"] = ["Write code first"]

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(task_id=1, success=True, output="Done"))
            self.assertIn("CANNOT REPORT SUCCESS: You have NOT written any code solution", res)
            self.assertIn("Write code first", res)
        finally:
            loop.close()

    def test_report_task_result_success_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["report_task_result"].function

        self.orchestrator._code_written_in_current_task = True
        self.orchestrator.control_flags["suggestions"] = ["Proceed to next problem"]

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(task_id=1, success=True, output="100% passed"))
            self.assertIn("Result recorded for Task #1.", res)
            self.assertIn("Proceed to next problem", res)
        finally:
            loop.close()

    def test_get_human_suggestions_active_and_empty(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["get_human_suggestions"].function

        loop = asyncio.new_event_loop()
        try:
            # When empty
            res1 = loop.run_until_complete(fn())
            self.assertIn("No custom suggestions from the human user", res1)

            # When active
            self.orchestrator.control_flags["suggestions"] = ["Optimize memory complexity"]
            res2 = loop.run_until_complete(fn())
            self.assertIn("Human User Direct Instructions retrieved", res2)
            self.assertIn("Optimize memory complexity", res2)
        finally:
            loop.close()

    def test_execute_learned_skill_page_none_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["execute_learned_skill"].function

        self.orchestrator.control_flags["suggestions"] = ["Handle modal carefully"]
        mock_browser = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(skill_name="handle_blocking_modals"))
            self.assertIn("Error: Active browser page is not available", res)
            self.assertIn("Handle modal carefully", res)
        finally:
            loop.close()

    def test_execute_learned_skill_exception_with_suggestions(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["execute_learned_skill"].function

        self.orchestrator.control_flags["suggestions"] = ["Recover from skill error"]
        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)
        self.orchestrator.skill_registry.execute_skill = AsyncMock(
            side_effect=RuntimeError("Skill execution crashed")
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(fn(skill_name="monaco_diff_injector"))
            self.assertIn("Skill 'monaco_diff_injector' execution failed: Skill execution crashed", res)
            self.assertIn("Recover from skill error", res)
        finally:
            loop.close()

    def test_stop_requested_raises_cancelled_in_actions(self):
        controller = self.orchestrator._build_controller()
        self.orchestrator.control_flags["stop_requested"] = True

        actions_to_test = [
            ("extract_problem_description", {}),
            ("describe_page_visuals", {"query": "test"}),
            ("extract_and_view_reference_images", {}),
            ("verify_submission_status", {}),
            ("get_human_suggestions", {}),
            ("write_code_to_editor", {"code": "code"}),
            ("submit_code_solution", {"code": "code"}),
            ("execute_learned_skill", {"skill_name": "skill"}),
            ("read_code_from_editor", {}),
            ("reset_editor", {}),
        ]

        loop = asyncio.new_event_loop()
        try:
            for action_name, kwargs in actions_to_test:
                fn = controller.registry.registry.actions[action_name].function
                with self.assertRaises(asyncio.CancelledError, msg=f"Action {action_name} should raise CancelledError when stop_requested"):
                    loop.run_until_complete(fn(**kwargs))
        finally:
            loop.close()



if __name__ == "__main__":
    unittest.main()

