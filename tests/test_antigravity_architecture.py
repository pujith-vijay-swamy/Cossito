"""
test_antigravity_architecture.py — Comprehensive test suite for 1:1 Antigravity /browser Architecture (Phases 1–4).

Verifies:
- Phase 1: TaskSubagent context isolation and TaskExecutionResult contracts.
- Phase 2: URLSecurityGate Layer 1 (Denylist), Layer 2 (Allowlist/Hierarchy), Auth exemptions, and Approval workflow.
- Phase 3: DevToolsPerceptionEngine AXTree extraction, console telemetry, network failure tracking, MultimodalObservation.
- Phase 4: BoundedRecoveryController multi-signal failure classification and DevTools recovery operations.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from devtools_perception import DevToolsPerceptionEngine
from models import (
    Budgets,
    CodingTask,
    FailureCategory,
    MultimodalObservation,
    PlatformTask,
    RecoveryDecision,
    RecoveryOperation,
    SecurityCheckResult,
    SecurityDecision,
    TaskExecutionResult,
    TaskState,
    TaskStatus,
)
from runtime_evolution import BoundedRecoveryController, classify_failure
from task_subagent import TaskSubagent
from url_security import URLSecurityGate


class TestAntigravityArchitecture(unittest.TestCase):

    # ------------------------------------------------------------------
    # Phase 1: Subagent Isolation Tests
    # ------------------------------------------------------------------

    def test_task_subagent_initialization(self):
        task = CodingTask(
            task_id="task-abc12345",
            queue_index=0,
            platform_task=PlatformTask(title="Two Sum"),
        )
        task.description = "Given nums and target, return indices."
        mock_orch = MagicMock()
        perception = DevToolsPerceptionEngine()

        subagent = TaskSubagent(task=task, orchestrator=mock_orch, perception_engine=perception)
        self.assertEqual(subagent.task.task_id, "task-abc12345")
        self.assertEqual(subagent.perception_engine, perception)

    def test_task_execution_result_contract(self):
        result = TaskExecutionResult(
            task_id="task-1",
            queue_index=0,
            success=True,
            skipped=False,
            solution_code="def solve(): pass",
            output_message="All test cases passed",
        )
        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.solution_code, "def solve(): pass")

    # ------------------------------------------------------------------
    # Phase 2: Two-Layer Security Gate Tests
    # ------------------------------------------------------------------

    def test_security_gate_layer1_denylist(self):
        gate = URLSecurityGate(target_base_url="https://mentorpick.com/course/123")
        # Prohibited ad/tracker domains
        res1 = gate.evaluate_url("https://doubleclick.net/ad/123")
        self.assertEqual(res1.decision, SecurityDecision.DENIED)

        res2 = gate.evaluate_url("https://googleadservices.com/pagead")
        self.assertEqual(res2.decision, SecurityDecision.DENIED)

    def test_security_gate_layer2_target_and_subdomains(self):
        gate = URLSecurityGate(target_base_url="https://mentorpick.com/course/123")
        
        # Exact target host
        res1 = gate.evaluate_url("https://mentorpick.com/course/123/problem/456")
        self.assertEqual(res1.decision, SecurityDecision.ALLOWED)

        # Subdomain of target
        res2 = gate.evaluate_url("https://app.mentorpick.com/api/test")
        self.assertEqual(res2.decision, SecurityDecision.ALLOWED)

    def test_security_gate_auth_exemptions(self):
        gate = URLSecurityGate(target_base_url="https://mentorpick.com/course/123")
        
        # Google OAuth
        res1 = gate.evaluate_url("https://accounts.google.com/o/oauth2/auth?client_id=xyz")
        self.assertEqual(res1.decision, SecurityDecision.ALLOWED)

        # GitHub OAuth
        res2 = gate.evaluate_url("https://github.com/login/oauth/authorize")
        self.assertEqual(res2.decision, SecurityDecision.ALLOWED)

        # General auth path
        res3 = gate.evaluate_url("https://auth.example.com/oauth/token")
        self.assertEqual(res3.decision, SecurityDecision.ALLOWED)

    def test_security_gate_unknown_external_domain_needs_approval(self):
        gate = URLSecurityGate(target_base_url="https://mentorpick.com/course/123")
        
        # Unknown external domain
        res = gate.evaluate_url("https://untrusted-external-site.org/redirect")
        self.assertEqual(res.decision, SecurityDecision.NEEDS_APPROVAL)
        self.assertEqual(res.domain, "untrusted-external-site.org")

        # After user approves for session
        gate.add_allowed_domain("untrusted-external-site.org", persistent=False)
        res_approved = gate.evaluate_url("https://untrusted-external-site.org/redirect")
        self.assertEqual(res_approved.decision, SecurityDecision.ALLOWED)

    # ------------------------------------------------------------------
    # Phase 3: DevTools Perception Engine Tests
    # ------------------------------------------------------------------

    async def _async_test_axtree_fallback(self):
        perception = DevToolsPerceptionEngine()
        mock_page = AsyncMock()
        mock_page.evaluate.return_value = {
            "nodes": [
                {"node_id": "1", "role": "button", "name": "Submit Code", "value": "", "description": "", "disabled": False, "focused": True},
                {"node_id": "2", "role": "textbox", "name": "Code Editor", "value": "int a = 5;", "description": "", "disabled": False, "focused": False}
            ],
            "interactive_elements": ["[BUTTON] 'Submit Code'", "[TEXTBOX] 'Code Editor'"]
        }

        snapshot = await perception.get_accessibility_tree(None, mock_page)
        self.assertEqual(len(snapshot.nodes), 2)
        self.assertEqual(snapshot.nodes[0].name, "Submit Code")
        self.assertTrue(snapshot.nodes[0].focused)
        self.assertEqual(len(snapshot.interactive_elements), 2)

    def test_axtree_fallback(self):
        asyncio.run(self._async_test_axtree_fallback())

    async def _async_test_multimodal_snapshot(self):
        perception = DevToolsPerceptionEngine()
        mock_page = AsyncMock()
        mock_page.get_url.return_value = "https://mentorpick.com/problem/1"
        mock_page.evaluate.side_effect = [
            "Title: MentorPick Problem 1 | Body Preview: Given an array...",  # DOM preview
            {
                "nodes": [{"node_id": "1", "role": "button", "name": "Run", "value": "", "description": "", "disabled": False, "focused": False}],
                "interactive_elements": ["[BUTTON] 'Run'"]
            },  # AXTree
            [{"level": "error", "message": "SyntaxError: Unexpected token", "source": "editor_runtime", "lineno": 12}],  # Console errors
            [{"name": "https://api.mentorpick.com/judge", "responseStatus": 500, "initiatorType": "fetch"}]  # Network failures
        ]

        obs = await perception.capture_multimodal_observation(None, mock_page, include_screenshot=False)
        self.assertEqual(obs.url, "https://mentorpick.com/problem/1")
        self.assertIn("MentorPick Problem 1", obs.dom_summary)
        self.assertEqual(len(obs.a11y_tree.nodes), 1)
        self.assertEqual(len(obs.console_errors), 1)
        self.assertEqual(obs.console_errors[0].text, "SyntaxError: Unexpected token")
        self.assertEqual(len(obs.network_failures), 1)
        self.assertEqual(obs.network_failures[0].status_code, 500)

    def test_multimodal_snapshot(self):
        asyncio.run(self._async_test_multimodal_snapshot())

    # ------------------------------------------------------------------
    # Phase 4: Self-Healing and Bounded Recovery Tests
    # ------------------------------------------------------------------

    def test_multi_signal_failure_classification(self):
        # 1. Error string pattern
        cat1 = classify_failure(Exception("Element with selector '.submit-btn' timed out"))
        self.assertEqual(cat1, FailureCategory.SELECTOR_TIMEOUT)

        # 2. Perception context console SyntaxError
        context_syntax = {
            "perception": {
                "console_errors": [{"text": "Uncaught SyntaxError: missing ) after argument list"}]
            }
        }
        cat2 = classify_failure(Exception("Unknown evaluation failure"), context=context_syntax)
        self.assertEqual(cat2, FailureCategory.COMPILATION)

        # 3. Perception context network 403
        context_auth = {
            "perception": {
                "network_failures": [{"status_code": 403, "url": "https://api.platform.com/submit"}]
            }
        }
        cat3 = classify_failure(Exception("Request failed"), context=context_auth)
        self.assertEqual(cat3, FailureCategory.AUTH_WALL)

    async def _async_test_bounded_recovery_axtree(self):
        controller = BoundedRecoveryController()
        budgets = Budgets(browser_recoveries=2)
        mock_page = AsyncMock()
        mock_page.evaluate.return_value = ["Submit", "Run Code", "Next"]

        res = await controller.attempt_recovery(
            error=Exception("Selector '#submit' timed out"),
            page=mock_page,
            budgets=budgets,
            context={"selector": "#submit"}
        )
        self.assertTrue(res.success)
        self.assertEqual(res.category, FailureCategory.SELECTOR_TIMEOUT)
        self.assertEqual(res.decision, RecoveryDecision.RETRY)
        self.assertEqual(budgets.browser_recoveries, 1)

    def test_bounded_recovery_axtree(self):
        asyncio.run(self._async_test_bounded_recovery_axtree())


if __name__ == "__main__":
    unittest.main()
