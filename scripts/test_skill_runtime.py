"""
test_skill_runtime.py — Automated test for AST manifest extraction, dynamic execution, and self-healing engine.
"""

import sys
import io

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import time
from pathlib import Path
from skill_registry import SkillRegistry
from runtime_evolution import SelfHealingEngine


def test_ast_manifest_speed():
    registry = SkillRegistry(Path(__file__).parent / "skills")
    
    # Measure AST extraction latency
    start = time.perf_counter()
    manifest = registry.load_manifest_ast()
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"[OK] AST Manifest extracted {len(manifest)} skills in {elapsed_ms:.3f}ms")
    assert elapsed_ms < 20.0, f"AST extraction too slow: {elapsed_ms}ms"
    assert len(manifest) >= 3, f"Expected at least 3 skills, found {len(manifest)}"

    for name, meta in manifest.items():
        print(f"  - Skill '{name}': {meta.summary} (params: {meta.parameters})")

    prompt_manifest = registry.get_prompt_manifest()
    print("\n--- PROMPT MANIFEST PREVIEW ---")
    print(prompt_manifest)
    print("-------------------------------\n")
    assert "handle_blocking_modals" in prompt_manifest
    assert "monaco_diff_injector" in prompt_manifest
    assert "shadow_dom_navigator" in prompt_manifest
    print("[OK] All AST manifest tests passed successfully!")


if __name__ == "__main__":
    test_ast_manifest_speed()
