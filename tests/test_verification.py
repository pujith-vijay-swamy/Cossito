"""
Unit tests for verification.py.
"""

import hashlib
import unittest
from models import CodingTask, SubmissionRecord, TaskState, VerificationResult
from verification import (
    build_verification_result,
    is_result_stale,
    is_submission_successful,
    parse_test_results,
    verify_submission,
)


class TestVerification(unittest.TestCase):
    def test_submission_success_formula(self):
        # 1. Terminal + total > 0 + passed == total + failed == 0 -> True
        rec1 = SubmissionRecord(
            terminal=True,
            total_count=5,
            passed_count=5,
            failed_count=0,
        )
        self.assertTrue(is_submission_successful(rec1))

        # 2. Not terminal -> False (even if numbers look good)
        rec2 = SubmissionRecord(
            terminal=False,
            total_count=5,
            passed_count=5,
            failed_count=0,
        )
        self.assertFalse(is_submission_successful(rec2))

        # 3. Partial pass (4/5) -> False
        rec3 = SubmissionRecord(
            terminal=True,
            total_count=5,
            passed_count=4,
            failed_count=1,
        )
        self.assertFalse(is_submission_successful(rec3))

        # 4. Total = 0 (no tests ran) -> False
        rec4 = SubmissionRecord(
            terminal=True,
            total_count=0,
            passed_count=0,
            failed_count=0,
        )
        self.assertFalse(is_submission_successful(rec4))

        # 5. Total is None -> False
        rec5 = SubmissionRecord(
            terminal=True,
            total_count=None,
            passed_count=0,
            failed_count=0,
        )
        self.assertFalse(is_submission_successful(rec5))

        # 6. Explicit outcome == 'accepted' -> True
        rec6 = SubmissionRecord(
            terminal=True,
            outcome="accepted",
        )
        self.assertTrue(is_submission_successful(rec6))

        # 7. Explicit outcome == 'rejected' -> False
        rec7 = SubmissionRecord(
            terminal=True,
            outcome="rejected",
            passed_count=5,
            total_count=5,
            failed_count=0,
        )
        # Note: If outcome is rejected, failed_count shouldn't be 0, but outcome overrides or counts check
        self.assertFalse(is_submission_successful(SubmissionRecord(terminal=True, outcome="rejected")))

    def test_is_result_stale(self):
        task = CodingTask()
        task.task_id = "task-12345"
        task.solution_code = "def solve(): return 100"

        code_hash = hashlib.sha256(task.solution_code.encode()).hexdigest()[:16]

        # Matching task and code hash -> NOT stale
        rec_good = SubmissionRecord(task_id="task-12345", code_hash=code_hash)
        self.assertFalse(is_result_stale(rec_good, task))

        # Mismatched task_id -> STALE
        rec_wrong_task = SubmissionRecord(task_id="task-99999", code_hash=code_hash)
        self.assertTrue(is_result_stale(rec_wrong_task, task))

        # Mismatched code_hash -> STALE
        rec_wrong_hash = SubmissionRecord(task_id="task-12345", code_hash="different_hash")
        self.assertTrue(is_result_stale(rec_wrong_hash, task))

    def test_parse_test_results(self):
        # 1. Standard "5/5 test cases passed"
        text1 = "Evaluation complete: 5/5 test cases passed. Congratulations!"
        rec1 = parse_test_results(text1)
        self.assertTrue(rec1.terminal)
        self.assertEqual(rec1.passed_count, 5)
        self.assertEqual(rec1.total_count, 5)
        self.assertEqual(rec1.failed_count, 0)

        # 2. Partial "3/5 test cases passed"
        text2 = "Results: 3/5 test cases passed. Failed test cases: 2"
        rec2 = parse_test_results(text2)
        self.assertTrue(rec2.terminal)
        self.assertEqual(rec2.passed_count, 3)
        self.assertEqual(rec2.total_count, 5)
        self.assertEqual(rec2.failed_count, 2)

        # 3. Running/Evaluating -> Not terminal
        text3 = "Running test cases... Please wait. Evaluating 0/5"
        rec3 = parse_test_results(text3)
        self.assertFalse(rec3.terminal)

        # 4. Accepted
        text4 = "Status: Accepted. Runtime: 45ms. Memory: 14MB"
        rec4 = parse_test_results(text4)
        self.assertTrue(rec4.terminal)
        self.assertEqual(rec4.outcome, "accepted")

        # 5. Wrong answer
        text5 = "Status: Wrong Answer. 4 / 12 test cases passed."
        rec5 = parse_test_results(text5)
        self.assertTrue(rec5.terminal)
        self.assertEqual(rec5.outcome, "wrong_answer")

        # 6. Score: 100/100
        text6 = "Submission Results: Score: 100/100. Status: Accepted"
        rec6 = parse_test_results(text6)
        self.assertTrue(rec6.terminal)
        self.assertEqual(rec6.outcome, "accepted")
        self.assertEqual(rec6.passed_count, 100)
        self.assertEqual(rec6.total_count, 100)
        self.assertEqual(rec6.failed_count, 0)

        # 7. Points: 10/10
        text7 = "Points: 10/10 test cases passed"
        rec7 = parse_test_results(text7)
        self.assertTrue(rec7.terminal)
        self.assertEqual(rec7.outcome, "accepted")
        self.assertEqual(rec7.passed_count, 10)
        self.assertEqual(rec7.total_count, 10)

    def test_post_write_snapshot_verification_workflow(self):
        """Verify that post-write snapshot hash correctly matches task.solution_code without stale error."""
        from models import WorkspaceSnapshot
        task = CodingTask()
        task.task_id = "task-recon-01"
        code = "def two_sum(nums, target): return [0, 1]"
        task.solution_code = code

        # Simulate post-write snapshot creation
        post_snap = WorkspaceSnapshot(editor_kind="monaco", content=code)
        post_snap.compute_hash()
        task.workspace = post_snap

        # Simulate submission record with matching hash
        record = SubmissionRecord(
            task_id=task.task_id,
            code_hash=post_snap.content_hash,
            terminal=True,
            outcome="accepted",
            passed_count=5,
            total_count=5,
            failed_count=0,
        )

        self.assertFalse(is_result_stale(record, task))
        success, v_result = verify_submission(record, task)
        self.assertTrue(success)
        self.assertEqual(v_result.functional, "passed")

    def test_verify_submission_populates_missing_task_id(self):
        """Verify that record without explicit task_id inherits task.task_id and passes verification."""
        task = CodingTask()
        task.task_id = "task-recon-02"
        record = SubmissionRecord(
            terminal=True,
            outcome="accepted",
            passed_count=3,
            total_count=3,
            failed_count=0,
        )
        self.assertEqual(record.task_id, "")
        success, v_result = verify_submission(record, task)
        self.assertTrue(success)
        self.assertEqual(record.task_id, "task-recon-02")
        self.assertEqual(v_result.functional, "passed")


if __name__ == "__main__":
    unittest.main()
