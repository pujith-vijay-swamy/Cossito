"""
skill_registry.py — Zero-Overhead Dynamic Skill Registry & Distillation Engine (v2).

Core capabilities:
1. AST-Based Manifest Extraction: Uses Python's native `ast` module to inspect docstrings
   and signatures of all scripts in `skills/` and `skills/approved/` in under 5ms.
   Quarantined skills are NEVER loaded into the active prompt or execution manifest.
2. Prompt Manifest Generator: Formats a clean, condensed summary of available approved skills.
3. Autonomous Skill Distillation with Quarantine: Distills runtime fixes into parameterized
   Python functions, subjects them to AST static review, and saves to `skills/quarantine/`.
4. Dynamic Native Execution: Loads and executes approved skills at native Python / Playwright speed.
5. Promotion Lifecycle: Exposes quarantine listing and manual/programmatic approval gates.

Schema version: 2.0
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from models import SkillCandidate
from skill_validation import (
    approve_skill as _approve_candidate,
    promote_to_approved,
    reject_skill as _reject_candidate,
    save_quarantined,
    static_review,
)

logger = logging.getLogger("omnitasker.skill_registry")


@dataclass
class SkillMetadata:
    """Metadata describing a distilled skill parsed via AST."""
    name: str
    file_path: Path
    docstring: str
    parameters: List[str] = field(default_factory=list)
    summary: str = ""
    last_modified: float = 0.0
    status: str = "approved"


class SkillRegistry:
    """Zero-overhead registry managing persistence, AST extraction, quarantine, and dynamic execution."""

    def __init__(self, skills_dir: Optional[Path] = None):
        if skills_dir is None:
            base_dir = Path(__file__).resolve().parent
            skills_dir = base_dir / "skills"

        self.skills_dir: Path = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        (self.skills_dir / "quarantine").mkdir(parents=True, exist_ok=True)
        (self.skills_dir / "approved").mkdir(parents=True, exist_ok=True)

        self._skills_cache: Dict[str, SkillMetadata] = {}
        self._loaded_modules: Dict[str, Any] = {}
        self._last_scan_time: float = 0.0

    def _scan_py_file(self, py_file: Path, status: str = "approved") -> Optional[SkillMetadata]:
        """Parse a single Python file using AST and extract metadata."""
        if py_file.name.startswith("__"):
            return None
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_name = node.name
                    if func_name.startswith("_"):
                        continue

                    docstring = ast.get_docstring(node) or ""
                    params = [
                        arg.arg for arg in node.args.args if arg.arg not in ("self", "cls")
                    ]

                    summary = (
                        docstring.strip().split("\n")[0]
                        if docstring
                        else f"Helper function {func_name}"
                    )
                    if len(summary) > 120:
                        summary = summary[:117] + "..."

                    skill_name = py_file.stem
                    return SkillMetadata(
                        name=skill_name,
                        file_path=py_file,
                        docstring=docstring,
                        parameters=params,
                        summary=summary,
                        last_modified=py_file.stat().st_mtime,
                        status=status,
                    )
        except Exception as e:
            logger.warning(f"Failed to parse skill AST for {py_file.name}: {e}")
        return None

    def load_manifest_ast(self) -> Dict[str, SkillMetadata]:
        """
        Fast AST-based extraction of active (approved + builtin) skills.
        Quarantined skills are explicitly excluded.
        """
        start_time = time.perf_counter()
        manifest: Dict[str, SkillMetadata] = {}

        if not self.skills_dir.exists():
            return manifest

        # 1. Builtin skills directly in skills/ (excluding subdirectories)
        for py_file in self.skills_dir.glob("*.py"):
            meta = self._scan_py_file(py_file, status="approved")
            if meta:
                manifest[meta.name] = meta

        # 2. Promoted skills in skills/approved/
        approved_dir = self.skills_dir / "approved"
        if approved_dir.exists():
            for py_file in approved_dir.glob("*.py"):
                meta = self._scan_py_file(py_file, status="approved")
                if meta:
                    manifest[meta.name] = meta

        self._skills_cache = manifest
        self._last_scan_time = time.perf_counter()
        elapsed_ms = (self._last_scan_time - start_time) * 1000
        logger.debug(f"AST skill scan loaded {len(manifest)} approved skills in {elapsed_ms:.2f}ms")
        return manifest

    def list_quarantined_skills(self) -> List[SkillCandidate]:
        """List all skill candidates currently in quarantine."""
        quarantine_dir = self.skills_dir / "quarantine"
        candidates: List[SkillCandidate] = []
        if not quarantine_dir.exists():
            return candidates

        for py_file in quarantine_dir.glob("*.py"):
            try:
                code = py_file.read_text(encoding="utf-8")
                candidate = SkillCandidate(
                    name=py_file.stem,
                    source_code=code,
                    status="quarantined",
                )
                static_review(candidate)
                candidates.append(candidate)
            except Exception as e:
                logger.warning(f"Failed to read quarantined skill {py_file.name}: {e}")
        return candidates

    def approve_skill(self, skill_name: str) -> bool:
        """Promote a quarantined skill to approved status."""
        quarantine_file = self.skills_dir / "quarantine" / f"{skill_name}.py"
        if not quarantine_file.exists():
            logger.warning(f"Quarantined skill '{skill_name}' does not exist")
            return False

        try:
            code = quarantine_file.read_text(encoding="utf-8")
            candidate = SkillCandidate(
                name=skill_name,
                source_code=code,
                status="quarantined",
            )
            static_review(candidate)
            if candidate.status != "reviewed":
                logger.warning(f"Skill '{skill_name}' failed review: {candidate.prohibited_ops}")
                return False

            _approve_candidate(candidate)
            promote_to_approved(candidate, self.skills_dir)
            self.load_manifest_ast()
            return True
        except Exception as e:
            logger.error(f"Failed to approve skill '{skill_name}': {e}")
            return False

    def reject_skill(self, skill_name: str, reason: str = "") -> bool:
        """Reject and delete a quarantined skill."""
        quarantine_file = self.skills_dir / "quarantine" / f"{skill_name}.py"
        if not quarantine_file.exists():
            return False
        try:
            quarantine_file.unlink()
            logger.info(f"Rejected and deleted quarantined skill '{skill_name}': {reason}")
            return True
        except Exception as e:
            logger.error(f"Failed to reject skill '{skill_name}': {e}")
            return False

    def get_prompt_manifest(self) -> str:
        """
        Produce a condensed Markdown block of approved skills for prompt injection.
        """
        manifest = self.load_manifest_ast()
        if not manifest:
            return "(No approved learned skills available)"

        lines = [
            "🛠️ AVAILABLE APPROVED SKILLS:",
            "You can invoke any of these verified skills using `execute_learned_skill(skill_name, parameters)`:",
        ]

        for name, meta in sorted(manifest.items()):
            params_str = ", ".join(meta.parameters)
            lines.append(f"- `{name}({params_str})`: {meta.summary}")

        lines.append("Use these stored skills to solve matching UI/DOM edge cases.\n")
        return "\n".join(lines)

    async def execute_skill(self, skill_name: str, page: Any, **kwargs) -> Any:
        """
        Dynamically loads and runs an approved skill function against the active browser page.
        """
        manifest = self.load_manifest_ast()
        if skill_name not in manifest:
            norm_name = skill_name.strip().replace("-", "_").lower()
            if norm_name in manifest:
                skill_name = norm_name
            else:
                available = ", ".join(manifest.keys()) if manifest else "None"
                raise ValueError(
                    f"Skill '{skill_name}' not found in approved registry. "
                    f"Available skills: {available}"
                )

        meta = manifest[skill_name]
        py_file = meta.file_path

        try:
            spec = importlib.util.spec_from_file_location(f"skills.{skill_name}", py_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not load module spec from {py_file}")

            module = importlib.util.module_from_spec(spec)
            sys.modules[f"skills.{skill_name}"] = module
            spec.loader.exec_module(module)
            self._loaded_modules[skill_name] = module

            target_func: Optional[Callable] = None
            if hasattr(module, skill_name) and callable(getattr(module, skill_name)):
                target_func = getattr(module, skill_name)
            elif hasattr(module, "run") and callable(getattr(module, "run")):
                target_func = getattr(module, "run")
            elif hasattr(module, "execute") and callable(getattr(module, "execute")):
                target_func = getattr(module, "execute")
            else:
                for attr_name in dir(module):
                    if not attr_name.startswith("_"):
                        attr = getattr(module, attr_name)
                        if callable(attr) and inspect.isfunction(attr):
                            target_func = attr
                            break

            if target_func is None:
                raise AttributeError(f"No executable function found in {py_file}")

            sig = inspect.signature(target_func)
            call_args: Dict[str, Any] = {}

            for param_name in sig.parameters:
                if param_name in ("page", "browser_page"):
                    call_args[param_name] = page
                elif param_name in kwargs:
                    call_args[param_name] = kwargs[param_name]

            if asyncio.iscoroutinefunction(target_func):
                result = await target_func(**call_args)
            else:
                result = target_func(**call_args)

            return result
        except Exception as e:
            logger.error(f"Error executing skill '{skill_name}': {e}", exc_info=True)
            raise

    async def distill_and_save_skill(
        self,
        skill_name: str,
        task_description: str,
        error_context: str,
        solution_code: str,
        llm: Any,
    ) -> Optional[Path]:
        """
        Uses the reasoning LLM to distill a one-off runtime patch into a candidate skill,
        subjects it to static AST review, and saves it to `skills/quarantine/`.
        """
        clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", skill_name.strip().lower())
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        if not clean_name:
            clean_name = f"skill_{int(time.time())}"

        from prompts import SKILL_DISTILLATION_TEMPLATE, _render

        prompt = _render(
            SKILL_DISTILLATION_TEMPLATE,
            task_description=task_description,
            error_context=error_context,
            solution_trace=solution_code,
            clean_name=clean_name,
        )

        try:
            from langchain_core.messages import HumanMessage
            message = HumanMessage(content=prompt)
            response = await llm.ainvoke([message])
            content = response.content if hasattr(response, "content") else str(response)

            code_match = re.search(r"```(?:python)?\s*\n(.*?)\n```", content, re.DOTALL)
            code_to_write = code_match.group(1).strip() if code_match else content.strip()

            candidate = SkillCandidate(
                name=clean_name,
                source_code=code_to_write,
                source_trace=f"Task: {task_description}\nError: {error_context}",
                status="quarantined",
            )

            # Static AST review
            static_review(candidate)
            if not candidate.ast_valid or not candidate.import_safe:
                logger.warning(
                    f"Distilled skill '{clean_name}' failed static review: {candidate.prohibited_ops}"
                )
                return None

            # Save to quarantine folder (NOT active skills folder)
            target_path = save_quarantined(candidate, self.skills_dir)
            logger.info(
                f"🛡️ Distilled skill '{clean_name}' passed static review and is QUARANTINED at {target_path}. "
                f"Requires approval before activation."
            )
            return target_path
        except Exception as e:
            logger.error(f"Failed to distill skill '{clean_name}': {e}", exc_info=True)
            return None
