"""
task_subagent.py — Subagent & Context Window Isolation for Cossito (Phase 1).

Executes each individual CodingTask in an isolated subagent context container:
- Sandboxed prompt scope and step budget.
- Clean context boundary: past task execution histories, thoughts, and DOM snapshots
  are NOT carried over into subsequent tasks in the queue, preventing token bloat.
- Emits a structured TaskExecutionResult with verified evidence and telemetry.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Optional

from browser_use import Agent
from devtools_perception import DevToolsPerceptionEngine
from dom_extraction import clean_extracted_problem_text, is_loading_placeholder, is_navigation_or_overview_text
from editor_adapters import safe_write
from models import (
    CodingTask,
    SubagentLifecycleState,
    SubmissionRecord,
    TaskExecutionResult,
    TaskState,
    TaskStatus,
    VerificationResult,
    WorkspaceSnapshot,
)
from platform_adapters import detect_platform, result_fingerprint
from prompts import get_execution_system_prompt, get_execution_task_prompt
from state_machine import can_transition, transition
from verification import is_submission_successful, parse_test_results, is_result_stale

logger = logging.getLogger(__name__)


class TaskSubagent:
    """Isolated execution subagent for solving a single CodingTask."""

    def __init__(
        self,
        task: CodingTask,
        orchestrator: Any,
        perception_engine: Optional[DevToolsPerceptionEngine] = None,
    ) -> None:
        self.task = task
        self.orchestrator = orchestrator
        self.perception_engine = perception_engine or DevToolsPerceptionEngine()
        self.lifecycle_state: SubagentLifecycleState = SubagentLifecycleState.SPAWNING
        self._current_agent: Optional[Agent] = None
        self._current_task_future: Optional[asyncio.Task] = None

    async def execute(self, total_tasks: int = 1) -> TaskExecutionResult:
        """Run the isolated task execution lifecycle and return a structured result."""
        self.lifecycle_state = SubagentLifecycleState.SPAWNING
        self.perception_engine.clear_telemetry()
        orch = self.orchestrator
        task = self.task
        task_id_num = task.queue_index + 1
        if str(task.task_id).replace("task-", "").isdigit():
            task_id_num = int(str(task.task_id).replace("task-", ""))

        # A verification timeout is not permission to generate or submit again.
        if task.submissions and not task.submissions[-1].terminal:
            orch._log('Checking the existing test result; preserving the current code.', 'info')
            try:
                async def inspect_existing():
                    browser = await orch.browser_mgr.get_browser()
                    page = await orch._get_current_page_safe(browser)
                    adapter = await detect_platform(page)
                    return await adapter.parse_result(page)
                record = await asyncio.wait_for(inspect_existing(), timeout=10.0)
                baseline = getattr(orch, '_submission_baseline', None)
                fresh = (baseline is None or getattr(orch, '_submission_observed_pending', False)
                         or result_fingerprint(record) != result_fingerprint(baseline))
                if not record.terminal:
                    orch._submission_observed_pending = True
                if record.terminal and fresh:
                    previous = task.submissions[-1]
                    record.task_id = previous.task_id
                    record.code_hash = previous.code_hash
                    task.submissions.append(record)
                    success = is_submission_successful(record) and not is_result_stale(record, task)
                    orch._task_completed_successfully = success
                    return TaskExecutionResult(task_id=str(task.task_id), success=success,
                                               output_message='Existing test result verified.' if success else 'Existing tests report a failure.')
            except Exception as exc:
                orch._log(f'Could not read the existing result: {exc}', 'warn')
            return TaskExecutionResult(task_id=str(task.task_id), success=False,
                                       output_message='The existing result is still unverified. Code and page were preserved.')

        orch._task_completed_successfully = False
        orch._code_written_in_current_task = False
        orch._user_skip_requested = False
        orch._last_report = None
        if hasattr(orch, "state") and orch.state is not None:
            orch.state.model_thinking = None

        browser = await orch.browser_mgr.get_browser()
        orch._log("Reading the selected task's full description before execution...", "info")
        async def read_statement():
            page = await orch._get_current_page_safe(browser)
            if page is None:
                raise RuntimeError('Selected task page is unavailable')
            text, _, _ = await orch._extract_problem_from_dom_direct(page)
            text = clean_extracted_problem_text(text)
            if (not text or is_loading_placeholder(text) or is_navigation_or_overview_text(text)
                    or 'Solve the coding problem for' in text
                    or text.strip().casefold() == (task.platform_task.title or '').strip().casefold()):
                raise RuntimeError('The full task description is unavailable; only a title or placeholder was found')
            return text
        try:
            task.description = await asyncio.wait_for(read_statement(), timeout=30.0)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            reason = str(exc) or 'Task description read timed out'
            orch._log(reason, 'error')
            return TaskExecutionResult(task_id=str(task.task_id), queue_index=task.queue_index,
                                       success=False, output_message=reason, error_log=[reason])
        orch._log(f"Full task description captured ({len(task.description)} characters): {task.description[:180]}", "success")
        llm = orch._get_llm_instance() if hasattr(orch, "_get_llm_instance") else None
        if llm is None:
            from llm_provider import get_llm
            llm = get_llm(model=orch.model, api_key=orch.api_key, provider=orch.provider, base_url=orch.base_url)

        controller = orch._build_controller()

        # 1. Fetch active human suggestions
        suggestions_list = []
        if orch.control_flags and orch.control_flags.get("suggestions"):
            suggestions_list = orch.control_flags["suggestions"].copy()
            orch.control_flags["suggestions"].clear()

        # Intercept human skip directives
        if suggestions_list:
            skip_keywords = ["skip", "next task", "go to next", "completed", "done", "ignore", "intro"]
            for sugg in suggestions_list:
                s_lower = sugg.lower()
                if any(kw in s_lower for kw in skip_keywords):
                    orch._log(f"⏭️ Human directive detected ('{sugg}'). Marking Task #{task_id_num} skipped/completed!", "success")
                    return TaskExecutionResult(
                        task_id=str(task.task_id),
                        queue_index=task.queue_index,
                        success=True,
                        skipped=True,
                        solution_code=task.solution_code,
                        output_message=f"Skipped per human directive: {sugg}",
                    )

        direct_suggestions = ""
        if suggestions_list:
            direct_suggestions = "\n\n🚨 ACTIVE HUMAN DIRECTIVES (YOU MUST FOLLOW THESE PRECISELY):\n" + "\n".join(f"- {s}" for s in suggestions_list)
            orch._log(f"💬 Injecting human suggestions directly into subagent prompt: {suggestions_list}", "success")

        # 2. Reference images handling for frontend tasks
        ref_images_prompt_section = ""
        initial_actions = []
        if orch._is_frontend_task(task) and getattr(browser, "is_cdp_connected", False):
            orch._log("🎨 Frontend task detected — extracting reference images...", "info")
            try:
                saved_images = await orch._extract_ref_images_to_disk()
                if saved_images:
                    paths_list = "\n".join(
                        f"  - {img['file_path']} (Alt: '{img['alt']}')" for img in saved_images
                    )
                    ref_images_prompt_section = (
                        f"\n\n🎨 REFERENCE IMAGES PRE-EXTRACTED (AUTOMATICALLY SAVED TO DISK):\n"
                        f"Inspect design mockups at:\n{paths_list}\n"
                    )
                else:
                    initial_actions = [{"read_code_from_editor": {}}]
                    orch._log("Reference check complete. Reading the editor and continuing from the written requirements.", "info")
                    ref_images_prompt_section = (
                        "\n\nREFERENCE CHECK COMPLETE: No usable reference images are available. "
                        "Do not repeat extraction or wait for a mockup. Read the editor boilerplate "
                        "and implement the written requirements now. Use reasonable design choices "
                        "where unspecified, and verify the rendered preview after coding.\n"
                    )
            except Exception as e:
                initial_actions = [{"read_code_from_editor": {}}]
                orch._log(f"⚠️ Pre-extraction of reference images note: {e}", "warn")
                ref_images_prompt_section = (
                    "\n\nReference inspection is unavailable. Continue from the written requirements; "
                    "do not wait for an image or repeat extraction.\n"
                )

        # 3. Build scoped system and task prompt (clean context window)
        budgets_dict = {
            "code_repairs": task.budgets.code_repairs,
            "submissions": task.budgets.submissions,
        }
        exec_sys = (
            get_execution_system_prompt(
                task.description,
                task_id_num,
                orch.use_vision,
                task.visual_design_spec,
                budgets=budgets_dict,
            )
            + ref_images_prompt_section
            + direct_suggestions
        )

        # Inject recent error history for targeted self-healing
        if task.error_log:
            recent_errors = task.error_log[-3:]
            error_context = "\n\n🔴 PREVIOUS ATTEMPT FAILURES (YOU MUST FIX THESE):\n"
            for i, err in enumerate(recent_errors):
                error_context += f"Attempt {i+1} error: {str(err)[:500]}\n"
            exec_sys += error_context

        exec_task = get_execution_task_prompt(
            task_id_num,
            task.description,
            task.target_selector,
            task.expected_behavior,
            total_tasks=total_tasks,
        )
        exec_task += (
            f"\nTask workspace: {task.platform_task.url or orch.url}\n"
            "Stay in this workspace. URLs in the statement are reference assets or "
            "requirements for the generated code, not destinations for the browser."
        )

        # 4. Instantiate isolated browser-use Agent
        agent = Agent(
            task=exec_task,
            # The orchestrator has already selected the task. Prevent URL
            # extraction from the prompt from scheduling an initial navigation.
            initial_actions=initial_actions,
            directly_open_url=False,
            max_failures=1,
            llm=llm,
            browser=browser,
            controller=controller,
            extend_system_message=exec_sys,
            # Recheck workspace scope between actions, including clicks that
            # unexpectedly open a reference or redirect the current tab.
            max_actions_per_step=1,
            register_new_step_callback=orch._on_agent_step,
            use_vision=orch.use_vision,
        )

        orch._current_agent = agent
        history = None
        recovered_success = False

        async def step_start(current_agent):
            orch._log("Preparing the current page and requesting the model's next action...", "info")

        async def step_end(current_agent):
            for result in getattr(current_agent.state, 'last_result', []) or []:
                if getattr(result, 'error', None):
                    orch._log(f"Execution step failed: {result.error}", "error")

        try:
            self.lifecycle_state = SubagentLifecycleState.RUNNING
            self._current_agent = agent
            self._current_task_future = asyncio.create_task(agent.run(
                max_steps=60, on_step_start=step_start, on_step_end=step_end,
            ))
            orch._current_agent_task = self._current_task_future
            history = await self._current_task_future
        except asyncio.CancelledError:
            if getattr(orch, "_task_completed_successfully", False) or (orch._last_report and orch._last_report.get("success")):
                recovered_success = True
            elif getattr(orch, "_user_skip_requested", False):
                self.lifecycle_state = SubagentLifecycleState.IDLE
                return TaskExecutionResult(
                    task_id=str(task.task_id),
                    queue_index=task.queue_index,
                    lifecycle_state=SubagentLifecycleState.IDLE,
                    success=True,
                    skipped=True,
                    solution_code=task.solution_code,
                    output_message="Task skipped by user request",
                )
            else:
                self.lifecycle_state = SubagentLifecycleState.ERRORED
                orch._log("🛑 Subagent task cancelled.", "warn")
                raise
        finally:
            if self.lifecycle_state == SubagentLifecycleState.RUNNING:
                self.lifecycle_state = SubagentLifecycleState.IDLE
            self._current_task_future = None
            orch._current_agent_task = None
            orch._current_agent = None
            self._current_agent = None

        # No code was changed: this is an execution/provider failure, not a code
        # repair. Return its real cause before optional browser diagnostics.
        errors = [str(e) for e in (history.errors() if history else []) if e]
        for action_result in getattr(getattr(agent, 'state', None), 'last_result', []) or []:
            error = getattr(action_result, 'error', None)
            if error and str(error) not in errors:
                errors.append(str(error))
        invalid_model_response = any(
            marker in error.lower() for error in errors
            for marker in ('json_invalid', 'invalid or incomplete json', 'invalid json')
        )
        if not recovered_success and not getattr(orch, '_task_completed_successfully', False) and (
            not getattr(orch, '_code_written_in_current_task', False) or invalid_model_response
        ):
            final_text = history.final_result() if history and callable(getattr(history, 'final_result', None)) else None
            reason = errors[-1] if errors else (
                f'Agent ended before verified execution: {final_text}' if final_text else
                'Agent returned without writing code and without a final explanation. No completion was verified.'
            )
            orch._log(reason, 'error')
            return TaskExecutionResult(task_id=str(task.task_id), queue_index=task.queue_index,
                                       success=False, error_log=errors, output_message=reason)

        # 5. Extract multimodal observation evidence via DevTools perception
        page = await orch._get_current_page_safe(browser)
        multimodal_obs = None
        if page is not None:
            try:
                multimodal_obs = await asyncio.wait_for(
                    self.perception_engine.capture_multimodal_observation(browser, page), timeout=10.0)
            except Exception as obs_err:
                logger.debug(f"[TaskSubagent] Multimodal observation capture note: {obs_err}")

        # Check fast-exit flag or report result
        if recovered_success or getattr(orch, "_task_completed_successfully", False) or (orch._last_report and orch._last_report.get("success")):
            return TaskExecutionResult(
                task_id=str(task.task_id),
                queue_index=task.queue_index,
                success=True,
                solution_code=task.solution_code,
                submissions=task.submissions,
                verification=task.verification,
                multimodal_evidence=multimodal_obs,
                output_message="Task completed and verified passing",
            )

        # Check for errors in history & invoke BoundedRecoveryController
        if history and history.has_errors():
            non_none_errors = [e for e in history.errors() if e is not None]
            if non_none_errors:
                last_error = non_none_errors[-1]
                orch._log(f"🩹 Triggering Bounded Recovery Controller for: {str(last_error)[:100]}...", "warn")
                try:
                    if page is not None:
                        rec_res = await orch.recovery_controller.attempt_recovery(
                            error=Exception(str(last_error)),
                            page=page,
                            budgets=task.budgets,
                            context={
                                "action": "solve_and_verify",
                                "task_description": task.description,
                                "perception": multimodal_obs.model_dump() if multimodal_obs else None,
                            },
                        )
                        if rec_res.success:
                            orch._log(f"✨ Recovery succeeded: {rec_res.diagnosis}", "success")
                except Exception as rec_err:
                    orch._log(f"Recovery attempt note: {rec_err}", "warn")
                task.error_log.append(f"Agent error: {last_error}")

        # Emergency code injection fallback if LLM generated code in response text
        if not getattr(orch, "_code_written_in_current_task", False) and history:
            try:
                final_res = history.final_result() or ""
                extracted = " ".join(history.extracted_content() or [])
                history_text = final_res + "\n" + extracted
                for h in history.history:
                    if hasattr(h, "model_output") and h.model_output:
                        thinking = getattr(h.model_output, "thinking", "") or ""
                        history_text += "\n" + thinking

                code_blocks = re.findall(r"```(?:python|javascript|java|cpp|c|ruby|go|rust|sql|html|css)?\s*\n(.*?)\n```", history_text, re.DOTALL | re.IGNORECASE)
                valid_codes = [c.strip() for c in code_blocks if len(c.strip()) > 30]
                if valid_codes and page is not None:
                    longest_code = max(valid_codes, key=len)
                    orch._log("⚡ Fallback: Injecting extracted solution code into editor...", "info")
                    success, snap, _ = await safe_write(page, longest_code)
                    if not success:
                        direct_res = await orch._inject_code_direct(browser, longest_code)
                        if direct_res:
                            success = True
                            snap = WorkspaceSnapshot(editor_kind="direct_injected", content=longest_code)
                    if success:
                        orch._code_written_in_current_task = True
                        task.solution_code = longest_code
                        task.workspace = snap
                        orch.state.current_solution_code = longest_code
                        orch._notify()
            except Exception as fb_err:
                logger.debug(f"[TaskSubagent] Fallback code injection note: {fb_err}")

        # Check submissions recorded
        if task.submissions and (is_submission_successful(task.submissions[-1]) and not is_result_stale(task.submissions[-1], task)):
            return TaskExecutionResult(
                task_id=str(task.task_id),
                queue_index=task.queue_index,
                success=True,
                solution_code=task.solution_code,
                submissions=task.submissions,
                verification=task.verification,
                multimodal_evidence=multimodal_obs,
                output_message="Submission confirmed accepted",
            )

        # Model prose, done(), and writing code are not submission evidence.
        # Only records verified by the submission actions can advance the queue.

        return TaskExecutionResult(
            task_id=str(task.task_id),
            queue_index=task.queue_index,
            success=False,
            solution_code=task.solution_code,
            submissions=task.submissions,
            verification=task.verification,
            error_log=task.error_log,
            multimodal_evidence=multimodal_obs,
            output_message="Task execution did not confirm passing test cases",
        )
