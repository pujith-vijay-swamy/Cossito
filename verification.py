from __future__ import annotations

import hashlib
import re
from typing import Optional, Tuple

from models import SubmissionRecord, VerificationResult, CodingTask, TaskState


def is_submission_successful(record: SubmissionRecord) -> bool:
    """
    Determine if a submission is completely successful based on empirical counts.
    
    Args:
        record: The SubmissionRecord to evaluate.
        
    Returns:
        True if the submission was accepted and passed all tests, False otherwise.
    """
    if not record.terminal:
        return False
    if record.outcome == 'accepted':
        return True
    if record.total_count is None or record.total_count == 0:
        return False
    if record.passed_count != record.total_count:
        return False
    if record.failed_count is not None and record.failed_count != 0:
        return False
    return True


def is_result_stale(record: SubmissionRecord, task: CodingTask) -> bool:
    """
    Check if a submission record is stale relative to the current task.
    
    Args:
        record: The SubmissionRecord to evaluate.
        task: The CodingTask to check against.
        
    Returns:
        True if the result is for a different task or older code, False otherwise.
    """
    if record.task_id and task.task_id and str(record.task_id).strip() != str(task.task_id).strip():
        return True
    
    if task.solution_code and record.code_hash:
        def _get_hashes(s: str) -> set[str]:
            h = hashlib.sha256(s.encode('utf-8')).hexdigest()
            return {h, h[:16]}

        code = task.solution_code
        valid_hashes = _get_hashes(code)
        if "\r\n" in code:
            valid_hashes |= _get_hashes(code.replace("\r\n", "\n"))
        if code.strip() != code:
            valid_hashes |= _get_hashes(code.strip())
            valid_hashes |= _get_hashes(code.replace("\r\n", "\n").strip())

        if record.code_hash not in valid_hashes:
            return True
            
    return False


def parse_test_results(raw_text: str) -> SubmissionRecord:
    """
    Parse unstructured platform output into a structured SubmissionRecord.
    
    Args:
        raw_text: The raw output text from the testing platform.
        
    Returns:
        A SubmissionRecord populated with extracted test outcomes.
    """
    text_lower = raw_text.lower()
    
    record = SubmissionRecord(
        terminal=True,
        raw_output=raw_text
    )
    
    # Check for active pending indicators only when not overridden by terminal results
    is_pending = bool(
        re.search(r'\b(evaluating|submitting|judging|pending|compiling|in\s+progress|queued)\b', text_lower)
        or re.search(r'\brunning\s+(?:test\s*cases?|tests?|code)\b', text_lower)
        or re.search(r'\bplease\s+wait\b', text_lower)
    )

    # Check for terminal status keywords
    has_accepted_kw = bool(
        re.search(r'\b(accepted|all\s+test\s*cases?\s+passed|all\s+tests?\s+passed|congratulations|correct\s+answer|status:\s*success|submission\s+successful|100%\s+passed|100%\s+test\s*cases?\s+passed)\b', text_lower)
    )
    has_failure_kw = bool(
        re.search(r'\b(wrong\s+answer|time\s+limit\s+exceeded|runtime\s+error|compilation\s+error)\b', text_lower)
    )

    if has_accepted_kw:
        record.outcome = "accepted"
    elif "wrong answer" in text_lower:
        record.outcome = "wrong_answer"
    elif "time limit exceeded" in text_lower:
        record.outcome = "time_limit_exceeded"
    elif "runtime error" in text_lower:
        record.outcome = "runtime_error"
    elif "compilation error" in text_lower:
        record.outcome = "compilation_error"
        record.terminal = True
        return record

    # Parse counts: passed vs total or passed vs failed
    # 1. Matches: "5/5 test cases passed", "5/5 passed", "5 of 5 passed", "5 out of 5 passed", "5/5 testcases passed"
    match = re.search(r'(\d+)\s*(?:/|out of|of)\s*(\d+)\s*(?:test\s*cases?|testcases?|tests?)?\s*passed', text_lower)
    if match:
        record.passed_count = int(match.group(1))
        record.total_count = int(match.group(2))
        record.failed_count = max(0, record.total_count - record.passed_count)

    # 2. Matches: "Passed: 5/5", "Test cases passed: 5/5"
    if record.passed_count is None:
        match = re.search(r'(?:passed|test\s*cases?\s*passed|tests?\s*passed)\s*:?\s*(\d+)\s*(?:/|out of|of)\s*(\d+)', text_lower)
        if match:
            record.passed_count = int(match.group(1))
            record.total_count = int(match.group(2))
            record.failed_count = max(0, record.total_count - record.passed_count)

    # 3. Matches: "passed: 5, failed: 0", "Passed: 3 Failed: 2"
    if record.passed_count is None:
        match = re.search(r'(?:passed|test\s*cases?\s*passed)\s*:?\s*(\d+)\s*[,;/]?\s*(?:failed|test\s*cases?\s*failed)\s*:?\s*(\d+)', text_lower)
        if match:
            record.passed_count = int(match.group(1))
            record.failed_count = int(match.group(2))
            record.total_count = record.passed_count + record.failed_count

    # 4. Matches: "5 passed, 0 failed", "5 passed 0 failed"
    if record.passed_count is None:
        match = re.search(r'(\d+)\s*passed\s*[,;/]?\s*(\d+)\s*failed', text_lower)
        if match:
            record.passed_count = int(match.group(1))
            record.failed_count = int(match.group(2))
            record.total_count = record.passed_count + record.failed_count

    # 5. Matches: "Total: 5, Passed: 5, Failed: 0"
    if record.passed_count is None:
        match = re.search(r'total\s*:?\s*(\d+)\s*[,;/]?\s*passed\s*:?\s*(\d+)\s*[,;/]?\s*failed\s*:?\s*(\d+)', text_lower)
        if match:
            record.total_count = int(match.group(1))
            record.passed_count = int(match.group(2))
            record.failed_count = int(match.group(3))

    # 6. Matches: "Score: 100/100", "Points: 10/10"
    if record.passed_count is None:
        match = re.search(r'\b(?:score|points?|marks?)\s*:?\s*(\d+)\s*(?:/|out of|of)\s*(\d+)', text_lower)
        if match:
            record.passed_count = int(match.group(1))
            record.total_count = int(match.group(2))
            record.failed_count = max(0, record.total_count - record.passed_count)

    # Evaluate outcome from empirical counts if available
    if record.total_count is not None and record.total_count > 0:
        if record.passed_count == record.total_count:
            record.outcome = "accepted"
            record.failed_count = 0
        elif record.failed_count is not None and record.failed_count > 0 and not record.outcome:
            record.outcome = "wrong_answer"
    elif record.passed_count is not None and record.failed_count == 0 and record.passed_count > 0:
        record.outcome = "accepted"
        record.total_count = record.passed_count

    # If accepted keyword is present, ensure counts are populated for downstream checks
    if record.outcome == "accepted":
        if record.total_count is None or record.total_count == 0:
            record.total_count = record.passed_count or 1
            record.passed_count = record.total_count
            record.failed_count = 0
        record.terminal = True

    # If pending keywords were found, but no terminal outcome/counts were discovered yet, mark not terminal
    if is_pending and not has_accepted_kw and not has_failure_kw and record.total_count is None:
        record.terminal = False
        return record

    return record


def build_verification_result(record: SubmissionRecord, task: CodingTask) -> VerificationResult:
    """
    Build a comprehensive VerificationResult from a submission.
    
    Args:
        record: The SubmissionRecord to verify.
        task: The task context.
        
    Returns:
        A VerificationResult containing functional state and details.
    """
    if is_result_stale(record, task):
        return VerificationResult(
            functional="unknown",
            details="Stale evidence"
        )
        
    if not record.terminal:
        return VerificationResult(
            functional="unknown",
            details="Evaluation not terminal"
        )
        
    if is_submission_successful(record):
        return VerificationResult(
            functional="passed",
            details="All test cases passed."
        )
        
    if record.total_count is not None and record.total_count > 0:
        if record.passed_count is not None and record.passed_count > 0:
            return VerificationResult(
                functional="partial",
                details=f"{record.passed_count}/{record.total_count} passed"
            )
            
    return VerificationResult(
        functional="failed",
        details="Submission failed or partial success not confirmed."
    )


def verify_submission(record: SubmissionRecord, task: CodingTask) -> Tuple[bool, VerificationResult]:
    """
    Verify a submission record, returning success status and a detailed result.
    
    Args:
        record: The SubmissionRecord to verify.
        task: The CodingTask context.
        
    Returns:
        A tuple of (success_boolean, verification_result).
    """
    if not record.task_id and task.task_id:
        record.task_id = task.task_id
    result = build_verification_result(record, task)
    success = is_submission_successful(record) and not is_result_stale(record, task)
    return success, result
