"""
skill_validation.py — Skill candidate screening and promotion gates.

Skills must pass static review (AST, imports, prohibited ops) before
promotion from quarantine to the approved registry. One successful
repair does NOT become an immediately active capability.

Schema version: 2.0
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Optional

from models import SkillCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prohibited patterns
# ---------------------------------------------------------------------------

PROHIBITED_IMPORTS: frozenset[str] = frozenset({
    "subprocess",
    "shutil",
    "socket",
    "http.client",
    "http.server",
    "ftplib",
    "smtplib",
    "ctypes",
    "multiprocessing",
    "webbrowser",
    "code",
    "codeop",
    "compileall",
    "importlib",
    "runpy",
})

PROHIBITED_CALLS: frozenset[str] = frozenset({
    "eval",
    "exec",
    "compile",
    "execfile",
    "__import__",
    "globals",
    "locals",
    "getattr",
    "setattr",
    "delattr",
    "open",  # filesystem writes
})

PROHIBITED_ATTR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"os\.system"),
    re.compile(r"os\.popen"),
    re.compile(r"os\.exec"),
    re.compile(r"os\.spawn"),
    re.compile(r"os\.remove"),
    re.compile(r"os\.unlink"),
    re.compile(r"os\.rmdir"),
    re.compile(r"os\.rename"),
    re.compile(r"os\.makedirs"),
    re.compile(r"shutil\."),
    re.compile(r"subprocess\."),
]


# ---------------------------------------------------------------------------
# AST visitor for static screening
# ---------------------------------------------------------------------------

class _SkillASTVisitor(ast.NodeVisitor):
    """Walk the AST and flag prohibited operations."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.imports: list[str] = []
        self.has_async_def: bool = False
        self.function_names: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)
            top = alias.name.split(".")[0]
            if top in PROHIBITED_IMPORTS:
                self.violations.append(f"Prohibited import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self.imports.append(module)
        top = module.split(".")[0]
        if top in PROHIBITED_IMPORTS:
            self.violations.append(f"Prohibited import: from {module}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check direct calls like eval(...), exec(...)
        if isinstance(node.func, ast.Name):
            if node.func.id in PROHIBITED_CALLS:
                self.violations.append(f"Prohibited call: {node.func.id}()")
        # Check attribute calls like os.system(...)
        elif isinstance(node.func, ast.Attribute):
            try:
                full = ast.unparse(node.func)
                for pat in PROHIBITED_ATTR_PATTERNS:
                    if pat.search(full):
                        self.violations.append(f"Prohibited attribute call: {full}()")
                        break
            except Exception:
                pass
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.has_async_def = True
        self.function_names.append(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_names.append(node.name)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def static_review(candidate: SkillCandidate) -> SkillCandidate:
    """Perform AST-based static review of a skill candidate.

    Sets candidate.ast_valid, import_safe, prohibited_ops, and
    updates status to 'reviewed' (if clean) or 'rejected' (if violations found).

    Returns the updated candidate.
    """
    # 1. Check AST validity
    try:
        tree = ast.parse(candidate.source_code)
        candidate.ast_valid = True
    except SyntaxError as e:
        candidate.ast_valid = False
        candidate.prohibited_ops.append(f"Syntax error: {e}")
        candidate.status = "rejected"
        logger.warning(f"Skill '{candidate.name}' rejected: syntax error: {e}")
        return candidate

    # 2. Walk AST for violations
    visitor = _SkillASTVisitor()
    visitor.visit(tree)

    candidate.prohibited_ops = visitor.violations.copy()
    candidate.import_safe = len(visitor.violations) == 0

    # 3. Check that there is at least one function definition
    if not visitor.function_names:
        candidate.prohibited_ops.append("No function definitions found")
        candidate.import_safe = False

    # 4. Set status
    if candidate.import_safe and candidate.ast_valid:
        candidate.status = "reviewed"
        logger.info(f"Skill '{candidate.name}' passed static review")
    else:
        candidate.status = "rejected"
        logger.warning(
            f"Skill '{candidate.name}' rejected: {candidate.prohibited_ops}"
        )

    return candidate


def approve_skill(candidate: SkillCandidate) -> SkillCandidate:
    """Promote a reviewed candidate to approved status.

    Only candidates with status='reviewed' can be approved.
    """
    if candidate.status != "reviewed":
        raise ValueError(
            f"Cannot approve skill '{candidate.name}' with status '{candidate.status}'. "
            f"Only 'reviewed' candidates can be approved."
        )
    candidate.status = "approved"
    logger.info(f"Skill '{candidate.name}' v{candidate.version} approved for production use")
    return candidate


def reject_skill(candidate: SkillCandidate, reason: str = "") -> SkillCandidate:
    """Explicitly reject a skill candidate."""
    candidate.status = "rejected"
    if reason:
        candidate.prohibited_ops.append(f"Manual rejection: {reason}")
    logger.info(f"Skill '{candidate.name}' rejected: {reason}")
    return candidate


def save_quarantined(candidate: SkillCandidate, skills_dir: Path) -> Path:
    """Save a candidate to the quarantine directory."""
    quarantine_dir = skills_dir / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    filepath = quarantine_dir / f"{candidate.name}.py"
    filepath.write_text(candidate.source_code, encoding="utf-8")
    logger.info(f"Saved quarantined skill '{candidate.name}' to {filepath}")
    return filepath


def promote_to_approved(candidate: SkillCandidate, skills_dir: Path) -> Optional[Path]:
    """Move an approved skill from quarantine to the approved directory.

    Returns the new filepath, or None if the candidate is not approved.
    """
    if candidate.status != "approved":
        logger.warning(f"Cannot promote unapproved skill '{candidate.name}'")
        return None

    approved_dir = skills_dir / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    filepath = approved_dir / f"{candidate.name}.py"
    filepath.write_text(candidate.source_code, encoding="utf-8")

    # Remove from quarantine if present
    quarantine_path = skills_dir / "quarantine" / f"{candidate.name}.py"
    if quarantine_path.exists():
        quarantine_path.unlink()

    logger.info(f"Promoted skill '{candidate.name}' to {filepath}")
    return filepath
