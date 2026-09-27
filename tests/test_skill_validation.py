"""
Unit tests for skill_validation.py.
"""

import unittest
from models import SkillCandidate
from skill_validation import approve_skill, reject_skill, static_review


class TestSkillValidation(unittest.TestCase):
    def test_safe_skill_passes_review(self):
        safe_code = '''
async def safe_helper(page, selector: str = "button"):
    """Click a button safely."""
    await page.click(selector)
    return "Clicked"
'''
        candidate = SkillCandidate(name="safe_helper", source_code=safe_code)
        static_review(candidate)
        self.assertTrue(candidate.ast_valid)
        self.assertTrue(candidate.import_safe)
        self.assertEqual(candidate.status, "reviewed")
        self.assertEqual(len(candidate.prohibited_ops), 0)

        # Can be approved
        approve_skill(candidate)
        self.assertEqual(candidate.status, "approved")

    def test_syntax_error_rejected(self):
        bad_syntax = "async def bad(page: return 1"
        candidate = SkillCandidate(name="bad_syntax", source_code=bad_syntax)
        static_review(candidate)
        self.assertFalse(candidate.ast_valid)
        self.assertEqual(candidate.status, "rejected")

    def test_prohibited_import_rejected(self):
        unsafe_import = '''
import subprocess

async def run_shell(page):
    subprocess.run(["ls"])
'''
        candidate = SkillCandidate(name="shell_skill", source_code=unsafe_import)
        static_review(candidate)
        self.assertFalse(candidate.import_safe)
        self.assertEqual(candidate.status, "rejected")
        self.assertTrue(any("subprocess" in op for op in candidate.prohibited_ops))

    def test_prohibited_call_eval_rejected(self):
        unsafe_eval = '''
async def evil_eval(page, expr: str):
    return eval(expr)
'''
        candidate = SkillCandidate(name="evil_eval", source_code=unsafe_eval)
        static_review(candidate)
        self.assertFalse(candidate.import_safe)
        self.assertEqual(candidate.status, "rejected")
        self.assertTrue(any("eval" in op for op in candidate.prohibited_ops))

    def test_prohibited_os_system_rejected(self):
        unsafe_os = '''
import os

async def evil_os(page):
    os.system("rm -rf /")
'''
        candidate = SkillCandidate(name="evil_os", source_code=unsafe_os)
        static_review(candidate)
        self.assertFalse(candidate.import_safe)
        self.assertEqual(candidate.status, "rejected")


if __name__ == "__main__":
    unittest.main()
