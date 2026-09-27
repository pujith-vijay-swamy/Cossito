"""
Unit tests for models.py (v2 contracts).
"""

import unittest
from models import (
    Budgets,
    BudgetExhausted,
    CodingTask,
    ControlCommand,
    FailureCategory,
    InvalidTransition,
    LogEntry,
    PlatformTask,
    RecoveryOperation,
    SkillCandidate,
    StatementEvidence,
    SubmissionRecord,
    TaskState,
    TaskStatus,
    WorkspaceSnapshot,
)


class TestModelsV2(unittest.TestCase):
    def test_coding_task_creation_and_defaults(self):
        task = CodingTask()
        self.assertTrue(task.task_id.startswith("task-"))
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.budgets.code_repairs, 5)
        self.assertEqual(task.budgets.browser_recoveries, 3)

    def test_v1_compatibility_properties(self):
        task = CodingTask()
        task.description = "Test problem statement"
        task.target_selector = "#editor"
        task.expected_behavior = "All pass"
        task.visual_design_spec = "Header 60px red"

        self.assertEqual(task.description, "Test problem statement")
        self.assertEqual(task.statement.raw_text, "Test problem statement")
        self.assertEqual(task.target_selector, "#editor")
        self.assertEqual(task.platform_task.navigation_hint, "#editor")
        self.assertEqual(task.expected_behavior, "All pass")
        self.assertEqual(task.visual_design_spec, "Header 60px red")

        # Test status mapping
        task.status = TaskStatus.IN_PROGRESS
        self.assertEqual(task.state, TaskState.PLANNING)
        task.status = TaskStatus.SUCCESS
        self.assertEqual(task.state, TaskState.PASSED)
        task.status = TaskStatus.FAILED
        self.assertEqual(task.state, TaskState.FAILED)

    def test_from_v1_dict(self):
        d = {
            "task_id": 3,
            "description": "Calculate sum of numbers",
            "target_selector": ".input-box",
            "expected_behavior": "0 failed",
            "visual_design_spec": "Mockup details",
        }
        task = CodingTask.from_v1_dict(d, queue_index=2)
        self.assertEqual(task.queue_index, 2)
        self.assertEqual(task.platform_task.key, "3")
        self.assertEqual(task.platform_task.title, "Question 3")
        self.assertEqual(task.description, "Calculate sum of numbers")
        self.assertEqual(task.target_selector, ".input-box")
        self.assertEqual(task.expected_behavior, "0 failed")
        self.assertEqual(task.visual_design_spec, "Mockup details")
        self.assertEqual(task.kind, "frontend")

    def test_workspace_snapshot_hashing(self):
        ws = WorkspaceSnapshot(content="def solve(): return 42")
        h1 = ws.compute_hash()
        self.assertTrue(len(h1) > 0)
        self.assertEqual(ws.content_hash, h1)

        ws2 = WorkspaceSnapshot(content="def solve(): return 42")
        h2 = ws2.compute_hash()
        self.assertEqual(h1, h2)

        ws3 = WorkspaceSnapshot(content="def solve(): return 43")
        h3 = ws3.compute_hash()
        self.assertNotEqual(h1, h3)

    def test_budgets_decrement_and_exhaustion(self):
        b = Budgets(code_repairs=2, browser_recoveries=1, submissions=1)
        self.assertTrue(b.has_code_repairs())
        b.use_code_repair()
        self.assertEqual(b.code_repairs, 1)
        b.use_code_repair()
        self.assertEqual(b.code_repairs, 0)
        self.assertFalse(b.has_code_repairs())
        with self.assertRaises(BudgetExhausted):
            b.use_code_repair()

        b.use_browser_recovery()
        with self.assertRaises(BudgetExhausted):
            b.use_browser_recovery()


if __name__ == "__main__":
    unittest.main()
