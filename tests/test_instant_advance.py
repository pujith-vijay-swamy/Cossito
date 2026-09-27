"""
Unit tests for instant deterministic post-submission advance in AgentOrchestrator.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_core import AgentOrchestrator
from models import CodingTask, PlatformTask, SubmissionRecord, TaskStatus


class TestInstantDeterministicAdvance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orchestrator = AgentOrchestrator(
            url="https://example.com/problems/1",
            api_key="test-api-key",
            headless=True,
        )

    async def test_report_task_result_success_triggers_advance(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["report_task_result"].function

        self.orchestrator._code_written_in_current_task = True
        self.orchestrator.state.model_thinking = "Rambling model thoughts..."

        # Create dummy agent task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        self.orchestrator._current_agent_task = mock_task

        res = await fn(task_id=1, success=True, output="All 5 tests passed")

        self.assertIn("Result recorded for Task #1", res)
        self.assertTrue(self.orchestrator._task_completed_successfully)
        self.assertIsNone(self.orchestrator.state.model_thinking)

        # Allow scheduled call_soon to execute
        await asyncio.sleep(0.01)
        mock_task.cancel.assert_called_once()

    async def test_verify_submission_status_success_triggers_advance(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["verify_submission_status"].function

        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)

        task = CodingTask(task_id="1", queue_index=0)
        self.orchestrator._current_task = task
        self.orchestrator.state.model_thinking = "Unnecessary thinking"

        mock_agent_task = MagicMock()
        mock_agent_task.done.return_value = False
        self.orchestrator._current_agent_task = mock_agent_task

        passed_record = SubmissionRecord(
            terminal=True,
            outcome="accepted",
            passed_count=5,
            total_count=5,
            failed_count=0,
            status_text="Accepted",
            raw_output="5/5 passed",
        )

        with patch("agent_core.detect_platform", new_callable=AsyncMock) as mock_detect:
            mock_adapter = MagicMock()
            mock_adapter.parse_result = AsyncMock(return_value=passed_record)
            mock_detect.return_value = mock_adapter

            res = await fn()

        self.assertIn("SUBMISSION VERIFIED SUCCESSFUL", res)
        self.assertTrue(self.orchestrator._task_completed_successfully)
        self.assertIsNone(self.orchestrator.state.model_thinking)
        self.assertEqual(task.status, TaskStatus.SUCCESS)

        # Allow scheduled call_soon to execute
        await asyncio.sleep(0.01)
        mock_agent_task.cancel.assert_called_once()

    async def test_submit_code_solution_success_triggers_advance(self):
        controller = self.orchestrator._build_controller()
        fn = controller.registry.registry.actions["submit_code_solution"].function

        mock_browser = MagicMock()
        mock_page = MagicMock()
        self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)
        self.orchestrator._get_current_page_safe = AsyncMock(return_value=mock_page)

        task = CodingTask(task_id="1", queue_index=0)
        self.orchestrator._current_task = task

        mock_agent_task = MagicMock()
        mock_agent_task.done.return_value = False
        self.orchestrator._current_agent_task = mock_agent_task

        passed_record = SubmissionRecord(
            terminal=True,
            outcome="accepted",
            passed_count=8,
            total_count=8,
            failed_count=0,
            status_text="Accepted",
            raw_output="8/8 passed",
        )

        with patch("agent_core.safe_write", new_callable=AsyncMock) as mock_write, \
             patch("agent_core.detect_platform", new_callable=AsyncMock) as mock_detect, \
             patch("agent_core.wait_for_terminal", new_callable=AsyncMock) as mock_wait:
            from models import WorkspaceSnapshot
            mock_write.return_value = (True, WorkspaceSnapshot(editor_kind="monaco", content="code"), "code")
            mock_adapter = MagicMock()
            mock_adapter.click_submit = AsyncMock(return_value=True)
            mock_detect.return_value = mock_adapter
            mock_wait.return_value = passed_record

            res = await fn(code="def solve(): return 42")

        self.assertIn("Fast-Path SUBMISSION VERIFIED SUCCESSFUL", res)
        self.assertTrue(self.orchestrator._task_completed_successfully)
        self.assertIsNone(self.orchestrator.state.model_thinking)
        self.assertEqual(task.status, TaskStatus.SUCCESS)

        await asyncio.sleep(0.01)
        mock_agent_task.cancel.assert_called_once()

    async def test_on_agent_step_cancels_immediately_when_completed(self):
        self.orchestrator._task_completed_successfully = True
        self.orchestrator._current_task = CodingTask(task_id="1")

        with self.assertRaises(asyncio.CancelledError):
            await self.orchestrator._on_agent_step(browser_state=None, model_output=None)

    async def test_on_agent_step_cancels_when_last_report_success(self):
        self.orchestrator._last_report = {"success": True}
        self.orchestrator._current_task = CodingTask(task_id="2")

        with self.assertRaises(asyncio.CancelledError):
            await self.orchestrator._on_agent_step(browser_state=None, model_output=None)

    async def test_solve_and_verify_returns_true_on_cancelled_after_success(self):
        task = CodingTask(task_id="1", queue_index=0)
        self.orchestrator._task_completed_successfully = True
        self.orchestrator._last_report = {"success": True}

        # Mock Agent run to raise CancelledError simulating instant cancellation
        with patch("task_subagent.Agent") as mock_agent_cls, \
             patch("llm_provider.get_llm"):
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(side_effect=asyncio.CancelledError("Task completed successfully."))
            mock_agent_cls.return_value = mock_agent

            mock_browser = MagicMock()
            mock_browser.is_cdp_connected = False
            mock_browser.cdp_url = "http://localhost:9222"
            self.orchestrator.browser_mgr.get_browser = AsyncMock(return_value=mock_browser)

            # Note that _solve_and_verify resets _task_completed_successfully at entry,
            # so simulate success happening during execution:
            async def run_side_effect(*args, **kwargs):
                self.orchestrator._task_completed_successfully = True
                raise asyncio.CancelledError("Task completed successfully.")

            mock_agent.run.side_effect = run_side_effect

            result = await self.orchestrator._solve_and_verify(task)

            self.assertTrue(result)
            self.assertEqual(task.status, TaskStatus.SUCCESS)
            self.assertTrue(self.orchestrator._code_written_in_current_task)

    async def test_thinking_suppressed_in_logs_after_success(self):
        self.orchestrator._task_completed_successfully = True
        initial_log_count = len(self.orchestrator.state.logs)

        self.orchestrator._log("Thinking: I should explore other buttons")
        self.orchestrator._log("Model thought: What is the next task?")
        self.assertEqual(len(self.orchestrator.state.logs), initial_log_count)

        # Standard non-thinking logs still permitted
        self.orchestrator._log("Finished current task.")
        self.assertEqual(len(self.orchestrator.state.logs), initial_log_count + 1)

    async def test_sequential_multi_task_execution_clears_stale_flags(self):
        """Verify that completing Task 1 does not prematurely abort Task 2 due to stale state."""
        from agent_core import _is_section_header_text
        task1 = CodingTask(task_id="1", queue_index=0)
        task2 = CodingTask(task_id="2", queue_index=1)

        # Simulate Task 1 completing successfully
        self.orchestrator._task_completed_successfully = True
        self.orchestrator._last_report = {"task_id": 1, "success": True, "output": "All passed"}
        self.orchestrator._code_written_in_current_task = True

        # Now start Task 2 via _execute_single_task or _solve_and_verify
        with patch.object(self.orchestrator, "_navigate_to_task_question", new_callable=AsyncMock), \
             patch.object(self.orchestrator, "_solve_and_verify", new_callable=AsyncMock) as mock_solve:
            mock_solve.return_value = True

            await self.orchestrator._execute_single_task(task2, total_tasks=2)

            # Check that _execute_single_task reset state for Task 2
            self.assertEqual(task2.status, TaskStatus.SUCCESS)
            mock_solve.assert_awaited_once_with(task2, total_tasks=2)

    def test_is_section_header_text_discrimination(self):
        """Verify _is_section_header_text accurately identifies section headers vs leaf coding problems."""
        from agent_core import _is_section_header_text

        section_headers = [
            "Practice Problems",
            "practice questions",
            "Chapter 1 - Introduction to Programming",
            "Arrays (5)",
            "Linked Lists (10)",
            "Dynamic Programming",
            "Week 2: Binary Search",
            "Module 3: Trees & Graphs",
            "All Questions",
            "5/5 completed",
            "3 problems",
        ]
        leaf_problems = [
            "Two Sum",
            "Print 1 to N - without for loop",
            "Sum of Digits",
            "Reverse a String",
            "Valid Parentheses",
            "Longest Substring Without Repeating Characters",
            "Merge K Sorted Lists",
            "1. Two Sum",
            "Question 2",
        ]

        for s in section_headers:
            self.assertTrue(_is_section_header_text(s), f"Expected '{s}' to be recognized as a section header")

        for p in leaf_problems:
            self.assertFalse(_is_section_header_text(p), f"Expected '{p}' to NOT be recognized as a section header")


if __name__ == "__main__":
    unittest.main()

