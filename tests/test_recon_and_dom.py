"""
test_recon_and_dom.py — Unit tests for DOM problem extraction, section header detection,
and navigation/overview text filtering.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock
from dom_extraction import (
    is_section_header_text,
    is_navigation_or_overview_text,
    clean_extracted_problem_text,
    extract_problem_title_from_text,
    parse_eval_result,
    extract_problem_from_dom,
)
from recon_engine import ReconEngine


class TestReconAndDom(unittest.TestCase):

    def test_observed_single_question_overrides_multiple_model_tasks(self):
        orch = MagicMock()
        orch.url = "https://example.test/exercise"
        orch._detected_section_questions = [{"text": "Sum Digits", "active": True}]
        queue = ReconEngine(orch)._build_queue_from_recon({"tasks": [
            {"task_id": 1, "description": "Sum the digits of the input integer."},
            {"task_id": 2, "description": "Calculate the digit sum of the integer."},
        ]})
        self.assertEqual(len(queue.tasks), 1)
        self.assertEqual(queue.tasks[0].platform_task.title, "Sum Digits")

    def test_duplicate_candidates_merge_evidence_before_indexing(self):
        tasks = ReconEngine(MagicMock())._build_tasks_from_candidates([
            {"text": "Sum Digits"},
            {"text": " sum   digits ", "active": True, "completed": True},
            {"text": "Count Digits"},
        ], "Sum Digits Description Given an integer, compute the sum of its digits.")
        self.assertEqual(len(tasks), 2)
        self.assertEqual([task.queue_index for task in tasks], [0, 1])
        self.assertEqual(tasks[0].state.value, "passed")
        self.assertIn("Given an integer", tasks[0].description)

    def test_same_title_at_distinct_destinations_is_not_merged(self):
        tasks = ReconEngine(MagicMock())._build_tasks_from_candidates([
            {"text": "Exercise", "href": "/exercise/1"},
            {"text": "Exercise", "href": "/exercise/2"},
        ], "")
        self.assertEqual(len(tasks), 2)

    def test_model_duplicate_statements_ignore_generated_task_numbers(self):
        orch = MagicMock()
        orch.url = "https://example.test/exercise"
        orch._detected_section_questions = []
        queue = ReconEngine(orch)._build_queue_from_recon({"tasks": [
            {"task_id": 1, "description": "Print 1 to N", "target_selector": "#editor"},
            {"task_id": 2, "description": "Print  1 to N", "target_selector": "#editor"},
            {"task_id": 3, "description": "Print N to 1", "target_selector": "#editor"},
        ]})
        self.assertEqual([task.description for task in queue.tasks], ["Print 1 to N", "Print N to 1"])
        self.assertEqual([task.queue_index for task in queue.tasks], [0, 1])

    def test_is_navigation_or_overview_text_user_dump(self):
        user_dump = (
            "MentorPick Home Problems Submissions Blogs Compete Courses Code My Doubts "
            "25pa1a45a1-vijay_swamy_ar Application Profile My Bookmarks My Orders My Attendance "
            "Show QR Theme Account Logout The Power of Repetition 9 of 26 Complete (34%) "
            "Loops in Java Keywords in Loops Nested Loops Input Reading when T test cases are given "
            "Input Reading when unknown number of test cases are given Pattern Printing -1 "
            "Pattern Printing -2 Pattern Printing -3 L4 - Practice Problems Previous Chapter: Making "
            "Decisions Course Next Chapter: Arrays - Your First Data ... The Power of Repetition "
            "Learn to master The Power of Repetition through Loops"
        )
        self.assertTrue(is_navigation_or_overview_text(user_dump))
        self.assertEqual(clean_extracted_problem_text(user_dump), "")

    def test_clean_extracted_problem_text_on_composite_sum_digits_dump(self):
        composite_dump = (
            "MentorPick Home Problems Submissions Blogs Compete Courses Code My Doubts "
            "25pa1a45a1-vijay_swamy_ar Application Profile My Bookmarks My Orders My Attendance "
            "Show QR Theme Account Logout The Power of Repetition 23 of 26 Complete (88%) "
            "Loops in Java Keywords in Loops Nested Loops Input Reading when T test cases are given "
            "Input Reading when unknown number of test cases are given Pattern Printing -1 "
            "Pattern Printing -2 Pattern Printing -3 L4 - Practice Problems "
            "Print 1 to N - without for loop Print N to 1 - without for loop "
            "Print N to 1 - without while loop Print Negative Series N to 1 Print M to N "
            "Print Even Numbers Print Alternating Sign Series Print Series - I Print Series - II "
            "Print Series - III Calculate Factorial Alternate Sign without Multiples of Four "
            "Print Amazing Series Print Odd Numbers - Multi-Test "
            "Print multiplication table for given integer N Sum Digits Count Digit One Are Digits Same "
            "Previous Chapter: Making Decisions Course Next Chapter: Arrays - Your First Data ... "
            "Description Submissions Sum Digits CAKEWALK Description Given a N digit number, may contain "
            "leading zeroes as well. Print the sum digits of number. Prohibited Keywords No prohibited "
            "keywords for this problem. Input Format First line contains number of test cases T. Second "
            "line contains N representing number of digits in given number. Third line contains actual "
            "number of N digits. Output Format Print the sum of digits of number. Each test case output "
            "should be on a separate line. Constraints 1 <= T <= 200 1 ≤ N ≤ 105 0 <= digit <= 9 "
            "Example Input 1 2 3 555 6 010101 Output 1 15 3 Explanation 1 Self Explanatory "
            "1 2 3 4 5 6 7 8 ⌄ ⌄ import java.util.*; import java.io.*; class Main { public static void "
            "main(String[] args) { System.out.println(\"Hello, World!\"); } } Test1 Run Submit Input Output Expected Output."
        )
        cleaned = clean_extracted_problem_text(composite_dump)
        self.assertTrue(cleaned.startswith("Sum Digits CAKEWALK Description Given a N digit number"))
        self.assertNotIn("MentorPick Home Problems", cleaned)
        self.assertNotIn("23 of 26 Complete", cleaned)
        self.assertNotIn("import java.util.*", cleaned)
        self.assertNotIn("Test1 Run Submit", cleaned)
        self.assertEqual(extract_problem_title_from_text(cleaned), "Sum Digits")

    def test_parse_eval_result_handles_browser_use_json_strings(self):
        import json
        json_obj = json.dumps({"text": "Sum Digits Description", "images": []})
        json_list = json.dumps([{"text": "Sum Digits", "completed": False, "active": True}])
        self.assertEqual(parse_eval_result(json_obj), {"text": "Sum Digits Description", "images": []})
        self.assertEqual(parse_eval_result(json_list), [{"text": "Sum Digits", "completed": False, "active": True}])
        self.assertIs(parse_eval_result("False"), False)
        self.assertIs(parse_eval_result("True"), True)

    async def _async_test_extract_problem_from_dom_with_json_strings(self):
        import json
        mock_page = AsyncMock()
        mock_page.evaluate.side_effect = [
            json.dumps({
                "text": (
                    "Sum Digits CAKEWALK Description Given a N digit number, may contain leading zeroes as well. "
                    "Input Format First line contains T. Output Format Print the sum of digits. "
                    "Constraints 1 <= T <= 200 Example Input 1 2 3 555 Output 1 15"
                ),
                "images": [],
            }),
            "False",
            json.dumps([
                {"text": "Print 1 to N - without for loop", "completed": True, "active": False},
                {"text": "Sum Digits", "completed": False, "active": True},
                {"text": "Count Digit One", "completed": False, "active": False},
            ]),
        ]
        raw_text, questions, images = await extract_problem_from_dom(mock_page)
        self.assertIn("Sum Digits CAKEWALK", raw_text)
        self.assertEqual(len(questions), 3)
        self.assertEqual(questions[1]["text"], "Sum Digits")
        self.assertTrue(questions[1]["active"])

    def test_extract_problem_from_dom_with_json_strings(self):
        import asyncio
        asyncio.run(self._async_test_extract_problem_from_dom_with_json_strings())

    def test_build_tasks_from_candidates_binds_to_active_question(self):
        engine = ReconEngine(MagicMock())
        candidates = [
            {"text": "Print 1 to N - without for loop", "completed": True, "active": False},
            {"text": "Sum Digits", "completed": False, "active": True},
            {"text": "Count Digit One", "completed": False, "active": False},
        ]
        desc = (
            "Sum Digits CAKEWALK Description Given a N digit number, may contain leading zeroes as well. "
            "Input Format First line contains T. Output Format Print the sum of digits."
        )
        tasks = engine._build_tasks_from_candidates(candidates, desc)
        self.assertEqual(len(tasks), 3)
        # First task is completed and has placeholder, NOT Sum Digits description
        self.assertIn("Solve the coding problem for Print 1 to N", tasks[0].description)
        # Second task (Sum Digits) gets the active extracted description
        self.assertIn("Given a N digit number", tasks[1].description)

    def test_is_navigation_or_overview_text_chrome_patterns(self):
        self.assertTrue(is_navigation_or_overview_text("Home Problems Submissions Blogs Compete Courses Profile Logout"))
        self.assertTrue(is_navigation_or_overview_text("9 of 26 Complete (34%) Table of Contents"))
        self.assertTrue(is_navigation_or_overview_text("Previous Chapter: Making Decisions Next Chapter: Arrays"))
        self.assertTrue(is_navigation_or_overview_text("Learn to master The Power of Repetition through Loops"))

    def test_is_navigation_or_overview_text_real_problems(self):
        two_sum = (
            "Given an array of integers nums and an integer target, return indices of the two numbers "
            "such that they add up to target. You may assume that each input would have exactly one solution.\n"
            "Input: nums = [2,7,11,15], target = 9\nOutput: [0,1]\nConstraints: 2 <= nums.length <= 10^4"
        )
        self.assertFalse(is_navigation_or_overview_text(two_sum))

        pattern_problem = (
            "Pattern Printing - 1\n"
            "Given an integer N, print a right-angled triangle pattern of asterisks of height N.\n"
            "Input Format: A single integer N.\nOutput Format: N lines containing asterisks."
        )
        self.assertFalse(is_navigation_or_overview_text(pattern_problem))

        frontend_task = (
            "Design a responsive pricing card component as shown in the reference image. "
            "The card should have a title, price, feature list, and a rounded 'Subscribe' button."
        )
        self.assertFalse(is_navigation_or_overview_text(frontend_task))

    def test_is_section_header_text(self):
        # Section headers with prefixes and category terms
        self.assertTrue(is_section_header_text("L4 - Practice Problems"))
        self.assertTrue(is_section_header_text("Practice Problems"))
        self.assertTrue(is_section_header_text("Lab 3: Practice Questions"))
        self.assertTrue(is_section_header_text("Section 2 - Problems"))
        self.assertTrue(is_section_header_text("Chapter 1: Basic Exercises"))
        self.assertTrue(is_section_header_text("L1 - Loops in Java"))
        self.assertTrue(is_section_header_text("All Problems"))
        self.assertTrue(is_section_header_text("10 Problems"))
        self.assertTrue(is_section_header_text("5/10 Completed"))

        # Leaf problems that should NOT be treated as section headers
        self.assertFalse(is_section_header_text("Pattern Printing -1"))
        self.assertFalse(is_section_header_text("Pattern Printing -2"))
        self.assertFalse(is_section_header_text("Two Sum"))
        self.assertFalse(is_section_header_text("Reverse a Linked List"))
        self.assertFalse(is_section_header_text("Sum of Digits"))

    async def _async_test_extract_guards_overview(self):
        mock_page = AsyncMock()
        user_dump = (
            "MentorPick Home Problems Submissions Blogs Compete Courses Code My Doubts "
            "25pa1a45a1-vijay_swamy_ar Application Profile My Bookmarks My Orders My Attendance "
            "Show QR Theme Account Logout The Power of Repetition 9 of 26 Complete (34%)"
        )
        mock_page.evaluate.return_value = {
            "text": user_dump,
            "images": []
        }
        raw_text, questions, images = await extract_problem_from_dom(mock_page)
        # Should be stripped and returned as empty string so recon marks it unhydrated
        self.assertEqual(raw_text, "")

    def test_extract_guards_overview(self):
        import asyncio
        asyncio.run(self._async_test_extract_guards_overview())

    def test_needs_nav_query_params(self):
        target_url = "https://mentorpick.com/coursev2/123/chapter/456?active=789&index=20"
        target_base = target_url.split('?')[0]
        has_target_params = "?" in target_url

        # Case 1: Browser is at base URL without active query param -> MUST navigate
        p_url_1 = "https://mentorpick.com/coursev2/123/chapter/456"
        needs_nav_1 = (
            not p_url_1
            or p_url_1 in ("about:blank", "chrome://newtab/")
            or (target_base and not p_url_1.startswith(target_base))
            or (has_target_params and ("?" not in p_url_1 or p_url_1 != target_url))
        )
        self.assertTrue(needs_nav_1)

        # Case 2: Browser is already at target URL with active param -> NO navigation needed
        p_url_2 = "https://mentorpick.com/coursev2/123/chapter/456?active=789&index=20"
        needs_nav_2 = (
            not p_url_2
            or p_url_2 in ("about:blank", "chrome://newtab/")
            or (target_base and not p_url_2.startswith(target_base))
            or (has_target_params and ("?" not in p_url_2 or p_url_2 != target_url))
        )
        self.assertFalse(needs_nav_2)

        # Case 3: Browser is at about:blank -> MUST navigate
        p_url_3 = "about:blank"
        needs_nav_3 = (
            not p_url_3
            or p_url_3 in ("about:blank", "chrome://newtab/")
            or (target_base and not p_url_3.startswith(target_base))
            or (has_target_params and ("?" not in p_url_3 or p_url_3 != target_url))
        )
        self.assertTrue(needs_nav_3)

    def test_task_queue_section_expansion_skips_header(self):
        # Verify that section headers are filtered out and leaf questions form the task queue
        scanned_questions = [
            {"text": "L4 - Practice Problems", "completed": False},
            {"text": "Pattern Printing -1", "completed": False},
            {"text": "Pattern Printing -2", "completed": True},
            {"text": "Pattern Printing -3", "completed": False},
        ]
        real_qs = [q for q in scanned_questions if not is_section_header_text(q.get("text", ""))]
        self.assertEqual(len(real_qs), 3)
        self.assertEqual(real_qs[0]["text"], "Pattern Printing -1")
        self.assertEqual(real_qs[1]["text"], "Pattern Printing -2")
        self.assertEqual(real_qs[2]["text"], "Pattern Printing -3")


if __name__ == "__main__":
    unittest.main()
