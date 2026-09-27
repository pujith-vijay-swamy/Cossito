"""
test_startup.py — Test AgentOrchestrator initialization and startup readiness.
"""

import asyncio
import os
import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from agent_core import AgentOrchestrator
from llm_provider import get_llm
from prompts import get_recon_system_prompt, get_execution_system_prompt
from skill_registry import SkillRegistry


def test_init():
    print("[1] Testing SkillRegistry...")
    reg = SkillRegistry()
    manifest = reg.load_manifest_ast()
    print(f"    Loaded {len(manifest)} skills.")

    print("[2] Testing Prompts with dynamic AST injection...")
    recon_prompt = get_recon_system_prompt(use_vision=True)
    assert "APPROVED SKILLS" in recon_prompt or "AVAILABLE" in recon_prompt
    exec_prompt = get_execution_system_prompt("Solve two sum", 1, True)
    assert "APPROVED SKILLS" in exec_prompt or "AVAILABLE" in exec_prompt
    print("    Prompts generated successfully.")

    print("[3] Testing AgentOrchestrator instantiation...")
    orch = AgentOrchestrator(
        url="https://example.com",
        api_key=os.environ.get("OPENROUTER_API_KEY", "sk-or-v1-test"),
        model="google/gemma-4-31b-it:free",
        provider="OpenRouter",
    )
    print("    AgentOrchestrator instantiated.")

    print("[4] Testing Controller construction...")
    controller = orch._build_controller()
    print(f"    Controller built successfully: {type(controller)}")

    print("[5] Testing LLM instantiation...")
    llm = get_llm(model="google/gemma-4-31b-it:free", api_key="sk-or-v1-test", provider="OpenRouter")
    print(f"    LLM created: {type(llm)}")

    print("[OK] All startup initialization checks passed!")


if __name__ == "__main__":
    test_init()
