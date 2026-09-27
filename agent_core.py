"""
agent_core.py — Core agent orchestration for OmniTasker.

Manages the two-phase flow:
  Phase 1 (Recon):  Navigate to the target URL → analyse the full page →
                     produce a structured TaskQueue.
  Phase 2 (Execute): Iterate through each CodingTask → clear + inject code →
                      run → verify → retry on failure.

A single shared Browser instance is used across both phases so that
cookies, DOM state, and session are preserved.

Compatible with browser-use v0.12.x API (flat Browser model, Controller
with @controller.action decorator, AgentHistoryList with final_result /
extracted_content / is_done).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import json
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Set browser-use per-action timeout to 1 hour (3600s) so human login/CAPTCHA pauses never trigger premature 180s timeouts
from browser_use import Agent, Browser, Controller
from browser_manager import BrowserManager

# ── Monkey-patch: make ActionModel tolerate extra fields from LLM outputs ──
from browser_use.tools.registry.views import ActionModel as _ActionModel
_ActionModel.model_config['extra'] = 'ignore'

# ── Monkey-patch: wrap DOMWatchdog.on_BrowserStateRequestEvent to prevent Navigation errors ──
try:
    from browser_use.browser.watchdogs.dom_watchdog import DOMWatchdog
    from browser_use.browser.views import BrowserStateSummary, PageInfo, SerializedDOMState

    _orig_on_browser_state_request = DOMWatchdog.on_BrowserStateRequestEvent

    async def on_BrowserStateRequestEvent(self, event):
        try:
            res = await _orig_on_browser_state_request(self, event)
            if res is not None:
                return res
        except Exception as e:
            import logging
            logging.getLogger("DOMWatchdog").warning(f"Wrapped DOMWatchdog.on_BrowserStateRequestEvent caught error: {e}")

        try:
            page_url = await self.browser_session.get_current_page_url()
        except Exception:
            page_url = ""

        return BrowserStateSummary(
            dom_state=SerializedDOMState(_root=None, selector_map={}),
            url=page_url,
            title="Recovery Page",
            tabs=[],
            screenshot=None,
            page_info=PageInfo(
                viewport_width=1280,
                viewport_height=720,
                page_width=1280,
                page_height=720,
                scroll_x=0,
                scroll_y=0,
                pixels_above=0,
                pixels_below=0,
                pixels_left=0,
                pixels_right=0,
            ),
            pixels_above=0,
            pixels_below=0,
            browser_errors=["Recovered from DOMWatchdog exception"],
            is_pdf_viewer=False,
            recent_events=None,
            pending_network_requests=[],
            pagination_buttons=[],
            closed_popup_messages=getattr(self.browser_session, "_closed_popup_messages", []).copy()
            if hasattr(self, "browser_session") and self.browser_session is not None
            else [],
        )

    DOMWatchdog.on_BrowserStateRequestEvent = on_BrowserStateRequestEvent
except Exception as _patch_err:
    print(f"Warning: Could not apply DOMWatchdog patch: {_patch_err}")
# ── End monkey-patch ──

from llm_provider import get_llm
from skill_registry import SkillRegistry
from runtime_evolution import BoundedRecoveryController, classify_failure
from state_machine import TaskStateMachine, transition, can_transition, is_terminal
from verification import verify_submission, is_submission_successful, is_result_stale, parse_test_results
from editor_adapters import detect_editor, safe_write, MonacoAdapter, EditorAdapter
from platform_adapters import detect_platform, wait_for_terminal
from human_loop import HumanInTheLoopController, ControlCommand
from controller_actions import build_controller, consume_suggestions_block
from overlay_manager import OverlayManager
from recon_engine import ReconEngine
from url_security import URLSecurityGate
from devtools_perception import DevToolsPerceptionEngine
from task_subagent import TaskSubagent
from dom_extraction import (
    is_section_header_text,
    is_navigation_or_overview_text,
    clean_extracted_problem_text,
    has_problem_statement_markers,
    extract_problem_from_dom,
    inject_code_to_editor,
    extract_ref_images_to_disk,
)
from models import (
    AgentPhase,
    AgentState,
    Budgets,
    CodingTask,
    ControlCommand,
    FailureCategory,
    LogEntry,
    MultimodalObservation,
    PlatformTask,
    SecurityCheckResult,
    SecurityDecision,
    SubmissionRecord,
    TaskExecutionResult,
    TaskQueue,
    TaskState,
    TaskStatus,
    WorkspaceSnapshot,
)
from prompts import (
    get_recon_system_prompt,
    get_execution_system_prompt,
    get_execution_task_prompt,
    get_recon_task_prompt,
)


def _now() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _make_log(msg: str, level: str = "info") -> LogEntry:
    return LogEntry(timestamp=_now(), level=level, message=msg)


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Best-effort extraction of a JSON object from LLM output."""
    if not text:
        return None
    
    # 1. Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2. JSON repair utility on full text
    try:
        from llm_provider import repair_json
        repaired = repair_json(text)
        return json.loads(repaired)
    except Exception:
        pass

    # 3. Find markdown ```json { ... } ``` fences (greedy match)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        block = m.group(1).strip()
        try:
            return json.loads(block)
        except Exception:
            try:
                from llm_provider import repair_json
                return json.loads(repair_json(block))
            except Exception:
                pass

    # 4. Find outermost { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        block = m.group(0).strip()
        try:
            return json.loads(block)
        except Exception:
            try:
                from llm_provider import repair_json
                return json.loads(repair_json(block))
            except Exception:
                pass

    return None


def _is_section_header_text(txt: str) -> bool:
    """Identify if a string represents a section header/category rather than a leaf problem."""
    if not txt:
        return False
    t = txt.strip()
    patterns = [
        r'^(practice\s*problems?|practice\s*questions?|problem\s*set|exercises?|assignments?|topics?|modules?|chapters?|sections?|challenges?|contests?|assessments?|homework|tutorials?|labs?|units?|lessons?|milestones?|sessions?)(?:[\s:\-–—\d\(\)\[\]#]+.*)?$',
        r'^(chapter|module|section|week|day|part|unit|lesson|milestone|session|lab|topic|level)\s*[\d#]+(?:[\s:\-–—]+.*)?$',
        r'^(all\s*problems?|problem\s*list|question\s*list|all\s*questions|practice|problems|questions|challenges)$',
        r'(\d+\s*problems?|\d+\s*questions?|\d+\s*tasks?|\d+\s*items?|\d+/\d+\s*completed)',
        r'^(arrays?|strings?|linked\s*lists?|stacks?(?:\s*and\s*queues?)?|queues?|trees?(?:\s*and\s*graphs?)?|graphs?|dynamic\s*programming|dp|recursion(?:\s*and\s*backtracking)?|backtracking|binary\s*search|greedy(?:\s*algorithms?)?|bit\s*manipulation|sorting(?:\s*and\s*searching)?|math(?:\s*and\s*logic)?|two\s*pointers|sliding\s*window|hash\s*tables?|heaps?|tries?)(?:[\s:\-–—\(\)\[\]\d]+.*)?$',
    ]
    return any(bool(re.search(p, t, re.IGNORECASE)) for p in patterns)


def model_has_vision(model: str, provider: str) -> bool:
    """Determine if the selected model has native vision capabilities."""
    model_lower = model.lower()
    provider_lower = provider.lower()
    
    # 1. Google Gemini provider standardly has native vision on all standard models
    if "gemini" in provider_lower:
        return True
        
    # 2. Check OpenAI model family
    if "openai" in provider_lower:
        if "gpt-4o" in model_lower or "gpt-4-vision" in model_lower or "o1" in model_lower or "o3" in model_lower:
            return True
            
    # 3. Check OpenRouter model family
    if "openrouter" in provider_lower:
        if "gemini" in model_lower:
            return True
        if "gpt-4o" in model_lower or "gpt-4-vision" in model_lower:
            return True
        if "claude-3" in model_lower or "claude-3.5" in model_lower:
            return True
        if "vision" in model_lower or "pixtral" in model_lower or "llama-3.2-vision" in model_lower or "llava" in model_lower:
            return True
            
    # 4. Check for local or HF models with specific vision indicators
    if "vision" in model_lower or "llava" in model_lower or "minicpm-v" in model_lower:
        return True
        
    return False


class AgentOrchestrator:
    """Drives the full Recon → Execute pipeline.

    Parameters
    ----------
    url : str
        The target web page URL.
    api_key : str
        OpenRouter API key.
    on_state_change : callable, optional
        Callback ``(AgentState) -> None`` invoked whenever the agent state
        changes.  Used by the Streamlit UI to push updates.
    hitl_controller : HumanInTheLoopController, optional
        Shared controller for pause/resume.  If not provided, one is created.
    headless : bool
        Whether to run the browser in headless mode (default False).
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        on_state_change: Optional[Callable[[AgentState], None]] = None,
        hitl_controller: Optional[HumanInTheLoopController] = None,
        headless: bool = False,
        model: str = "google/gemma-4-31b-it:free",
        provider: str = "OpenRouter",
        use_vision: bool = True,
        control_flags: Optional[dict] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self._on_state_change = on_state_change
        self.hitl = hitl_controller or HumanInTheLoopController()
        self.browser_mgr = BrowserManager(headless=headless)
        self.state = AgentState()
        self._stop_requested = False
        self._task_completed_successfully = False
        self._code_written_in_current_task = False
        self._user_skip_requested = False
        self._current_agent_task: Optional[asyncio.Task] = None
        self._current_agent: Optional[Any] = None
        self._last_report: Optional[dict[str, Any]] = None
        self.model = model
        self.provider = provider
        self.use_vision = use_vision
        self.control_flags = control_flags if control_flags is not None else {"stop_requested": False}
        if "suggestions" not in self.control_flags:
            self.control_flags["suggestions"] = []
        if "stop_requested" not in self.control_flags:
            self.control_flags["stop_requested"] = False
        self.base_url = base_url
        self.skill_registry = SkillRegistry()
        self.recovery_controller = BoundedRecoveryController()
        self.self_healing_engine = self.recovery_controller
        self.state_machine = TaskStateMachine()
        self._current_task: Optional[CodingTask] = None
        self._active_submission: Optional[SubmissionRecord] = None
        self._injecting_pages: set[Any] = set()
        # Security Gate & DevTools Perception Engine (Phases 2 & 3)
        self.security_gate = URLSecurityGate(target_base_url=self.url)
        self.perception_engine = DevToolsPerceptionEngine()
        # Delegates for extracted modules
        self._overlay_mgr: Optional[OverlayManager] = OverlayManager(self)
        self._recon_engine: Optional[ReconEngine] = ReconEngine(self)

    @property
    def overlay_mgr(self) -> OverlayManager:
        if not hasattr(self, "_overlay_mgr") or self._overlay_mgr is None:
            self._overlay_mgr = OverlayManager(self)
        return self._overlay_mgr

    @overlay_mgr.setter
    def overlay_mgr(self, val: OverlayManager) -> None:
        self._overlay_mgr = val

    @property
    def recon_engine(self) -> ReconEngine:
        if not hasattr(self, "_recon_engine") or self._recon_engine is None:
            self._recon_engine = ReconEngine(self)
        return self._recon_engine

    @recon_engine.setter
    def recon_engine(self, val: ReconEngine) -> None:
        self._recon_engine = val

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> AgentState:
        """Execute the full pipeline: Recon → Execute → Done."""
        poll_task = None
        try:
            self._log("🚀 Agent starting…")
            self._cleanup_ref_images()
            
            # Start background polling task for overlay commands and state sync
            poll_task = asyncio.create_task(self._background_poll_loop())
            
            # Check if the user requested model has native vision capabilities
            has_vision = model_has_vision(self.model, self.provider)
            if has_vision:
                self.use_vision = True
                self._log(
                    f"👁️ Vision-capable model auto-detected ('{self.model}'). Native vision enabled with full access!",
                    "success"
                )
            else:
                self.use_vision = False
                self._log(
                    f"⚠️ Visionless model/provider detected ('{self.model}' / '{self.provider}'). "
                    f"Disabled native vision to prevent API errors. Gemma 4 vision assistant is active as a tool!",
                    "warn"
                )
                
            self._set_phase(AgentPhase.RECON)

            # Retry recon up to 3 times for transient browser/CDP or LLM provider errors (503, 429, etc.)
            _TRANSIENT_ERRORS = (
                "CDP client not initialized",
                "Expected at least one handler to return a non-None result",
                "WebSocket connection closed",
                "browser may not be connected",
                "Navigation failed",
                "503",
                "UNAVAILABLE",
                "high demand",
                "spikes in demand",
                "429",
                "RESOURCE_EXHAUSTED",
                "rate limit",
                "RateLimit",
                "quota",
                "500",
                "502",
                "504",
                "Bad Gateway",
                "Gateway Timeout",
                "Internal Server Error",
                "timed out",
                "TimeoutError",
                "Connection error",
                "APIConnectionError",
                "ServiceUnavailable",
            )
            task_queue = None
            for _recon_attempt in range(4):
                try:
                    task_queue = await self._run_recon()
                    break  # Success
                except Exception as _recon_err:
                    err_str = str(_recon_err)
                    is_transient = any(t.lower() in err_str.lower() for t in _TRANSIENT_ERRORS)
                    if is_transient and _recon_attempt < 3:
                        wait_secs = 5 * (_recon_attempt + 1)
                        self._log(
                            f"⚠️ Transient error detected (attempt {_recon_attempt + 1}/4): {err_str[:120]}... Retrying in {wait_secs}s.",
                            "warn",
                        )
                        # Only force browser recreation on browser/CDP crashes, not LLM rate limits
                        is_browser_error = any(t in err_str for t in ("CDP client", "WebSocket", "browser may not", "Navigation failed"))
                        if is_browser_error:
                            try:
                                await self.browser_mgr.close()
                            except Exception:
                                pass
                            self.browser_mgr._browser = None
                        await asyncio.sleep(wait_secs)
                    else:
                        raise  # Non-transient or final attempt — propagate

            if task_queue is None or not task_queue.tasks:
                self._log("⚠️ Recon produced no tasks.", "warn")
                self._set_phase(AgentPhase.DONE)
                return self.state

            self.state.task_queue = task_queue
            self._notify()
            self._log(
                f"📋 Recon complete — {len(task_queue.tasks)} task(s) found.",
                "success",
            )

            self._set_phase(AgentPhase.EXECUTING)
            await self._run_execution_loop(task_queue)

            self._set_phase(AgentPhase.DONE)
            self._log("✅ All tasks processed.", "success")

        except asyncio.CancelledError:
            self._log("🛑 Agent cancelled.", "warn")
            self._set_phase(AgentPhase.ERROR)
            self.state.error_message = "Cancelled by user."
        except Exception as exc:
            tb = traceback.format_exc()
            try:
                print(f"AgentOrchestrator run() caught exception: {exc}\n{tb}")
            except UnicodeEncodeError:
                # Fallback to safe printing if the console doesn't support the characters
                print(f"AgentOrchestrator run() caught exception: {repr(exc)}\n{tb.encode('ascii', 'replace').decode('ascii')}")
            self._log(f"💥 Fatal error: {exc}", "error")
            self._set_phase(AgentPhase.ERROR)
            self.state.error_message = f"{exc}\n{tb}"
        finally:
            # We want to keep the browser open if:
            # 1. A fatal error occurred
            # 2. Or no tasks were found during recon (indicates navigation/loading failure)
            # 3. Or any tasks in the queue failed to solve
            has_failed_tasks = False
            task_count = 0
            if self.state.task_queue:
                task_count = len(self.state.task_queue.tasks)
                has_failed_tasks = any(t.status == TaskStatus.FAILED for t in self.state.task_queue.tasks)

            keep_open = (
                (self.state.phase == AgentPhase.ERROR 
                 or task_count == 0 
                 or has_failed_tasks)
                and not self.control_flags.get("stop_requested")
                and not self._stop_requested
            )

            if keep_open:
                self._log("ℹ️ Keeping the browser open for manual inspection. Click 'Stop' in the overlay or sidebar to close.", "info")
                # Block the main run() coroutine to keep the event loop alive
                # and let the background poll loop continue running.
                while not self._stop_requested and not self.control_flags.get("stop_requested"):
                    try:
                        if not self.browser_mgr.is_open:
                            break
                        browser = await self.browser_mgr.get_browser()
                        if not browser.is_cdp_connected:
                            break
                        # Also check if any page is still open
                        pages = await browser.get_pages()
                        if not pages:
                            break
                    except Exception:
                        break
                    await asyncio.sleep(1.0)
                
            # Clean up extracted reference images
            self._cleanup_ref_images()

            self._log("Cleaning up — closing browser...", "info")
            try:
                await self.browser_mgr.close()
            except Exception:
                pass
            if poll_task is not None:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass

        return self.state

    def request_stop(self) -> None:
        """Signal the orchestrator to stop after the current step."""
        self._stop_requested = True
        self.control_flags["stop_requested"] = True

    # ------------------------------------------------------------------
    # Phase 1: Reconnaissance
    # ------------------------------------------------------------------

    async def _extract_problem_from_dom_direct(
        self, page, user_selector: Optional[str] = None
    ) -> tuple[str, list[dict], list[dict]]:
        """Directly extract problem statement and section questions from DOM via CDP."""
        return await extract_problem_from_dom(page, user_selector=user_selector)

    async def _defensively_recover_dom_tq(self, browser, current_url: str) -> Optional[TaskQueue]:
        """Defensively recover problem statement and tasks directly from DOM after an agent/LLM error."""
        return await self.recon_engine.defensively_recover_dom_tq(browser, current_url)

    async def _run_recon(self) -> Optional[TaskQueue]:
        """Navigate to the URL and produce a TaskQueue."""
        return await self.recon_engine.run_recon()

    # ------------------------------------------------------------------
    # Phase 2: Execution Loop
    # ------------------------------------------------------------------

    async def _run_execution_loop(self, tq: TaskQueue) -> None:
        """Iterate through each task and solve it."""
        total = len(tq.tasks)
        import time

        for idx, task in enumerate(tq.tasks):
            if self._stop_requested:
                self._log("🛑 Stop requested — aborting loop.", "warn")
                break

            self.state.current_task_index = idx
            self.state.progress_pct = idx / total
            self._notify()

            # ---- HITL check (use the task's expected URL context) ----
            await self._hitl_gate(self.url)

            # If task was already completed on platform, skip to next
            if task.status == TaskStatus.SUCCESS and task.state == TaskState.PASSED:
                self._log(f"⏩ {task.platform_task.title or f'Task #{idx + 1}'} already completed on platform. Advancing to next.", "info")
                continue

            # ---- Solve the task ----
            await self._execute_single_task(task, total_tasks=total)

            # ---- Check for graceful Pause Request ----
            if not hasattr(self, "control_flags") or self.control_flags is None:
                self.control_flags = {"stop_requested": False, "suggestions": []}
            
            if self.control_flags.get("pause_requested"):
                click_time = self.control_flags.get("pause_click_time", time.time())
                elapsed = time.time() - click_time
                self.control_flags["pause_elapsed_time"] = elapsed
                self.control_flags["pause_requested"] = False  # Consume request

                self._log(f"⏸️ Pause initiated. Single task completed in {elapsed:.2f} seconds after clicking pause.", "warn")
                self._set_phase(AgentPhase.PAUSED_HITL)
                self.state.hitl_reason = f"Agent gracefully paused by user. Task completed in {elapsed:.2f} seconds."
                self._notify()

                # Trigger pause until Resume button clicked
                await self.hitl.trigger_pause(self.state.hitl_reason)

                # Resume checks
                self._set_phase(AgentPhase.EXECUTING)
                self.state.hitl_reason = None
                self.control_flags["pause_elapsed_time"] = None
                self._log("▶️ Resuming agent loop. Checking for human suggestions...", "info")
                self._notify()

                # Immediately check suggestions
                suggs = self.control_flags.get("suggestions", []).copy()
                if suggs:
                    self._log(f"💬 Checked suggestions upon resume: Found and active: {suggs}", "success")
                else:
                    self._log("💬 Checked suggestions upon resume: None injected.", "info")

        self.state.progress_pct = 1.0
        self._notify()
        self._cleanup_ref_images()

    async def _navigate_to_task_question(self, task: CodingTask, total_tasks: int) -> None:
        """Programmatically attempt to switch to the current question/tab on the page if multi-tasking."""
        return await self.recon_engine.navigate_to_task_question(task, total_tasks)

    async def _execute_single_task(self, task: CodingTask, total_tasks: int = 1) -> None:
        """Solve, verify, and retry a single CodingTask with deterministic state transitions."""
        self._task_completed_successfully = False
        self._code_written_in_current_task = False
        self._user_skip_requested = False
        self._last_report = None
        if hasattr(self, "state") and self.state is not None:
            self.state.model_thinking = None
        self._current_task = task
        task_label = task.platform_task.title or f"Task #{task.queue_index + 1}"
        if task.description and is_navigation_or_overview_text(task.description):
            cleaned_init = clean_extracted_problem_text(task.description)
            if cleaned_init:
                task.description = cleaned_init

        desc_preview = (task.description or "").split("\n")[0][:80]
        self._log(f"🔧 {task_label} ({task.queue_index + 1}/{total_tasks}): {desc_preview}")

        # Transition QUEUED -> RECONCILING -> READY -> PLANNING
        if can_transition(task, TaskState.RECONCILING):
            transition(task, TaskState.RECONCILING)
        if can_transition(task, TaskState.READY):
            transition(task, TaskState.READY)
        if can_transition(task, TaskState.PLANNING):
            transition(task, TaskState.PLANNING)

        self._notify()

        # Programmatically auto-navigate browser to the current question tab if multi-tasking
        await self._navigate_to_task_question(task, total_tasks)

        # Re-extract problem description from DOM for tasks with placeholder descriptions
        is_placeholder = (
            "Solve the coding problem for" in (task.description or "")
            or (task.description or "").strip().endswith("Coding challenge")
            or len((task.description or "").strip()) < 40
            or is_navigation_or_overview_text(task.description or "")
        )
        if is_placeholder:
            try:
                browser = await self.browser_mgr.get_browser()
                page = await self._get_current_page_safe(browser)
                if page is not None:
                    # Poll up to 3 times after switching question tabs to ensure SPA problem statement is re-extracted
                    for poll_i in range(3):
                        await asyncio.sleep(0.8)
                        extracted_text, _, _ = await self._extract_problem_from_dom_direct(page)
                        extracted_text = clean_extracted_problem_text(extracted_text)
                        if extracted_text and len(extracted_text) > 30 and not is_navigation_or_overview_text(extracted_text) and not extracted_text.strip().endswith("Coding challenge"):
                            task.description = f"{task_label}. {extracted_text}" if not extracted_text.lower().startswith(task_label.lower()) else extracted_text
                            self._log(f"📝 Re-extracted problem description for {task_label} ({len(task.description)} chars)", "success")
                            self._notify()
                            break
                    if len((task.description or "").strip()) < 40 or "Solve the coding problem for" in (task.description or "") or is_navigation_or_overview_text(task.description or ""):
                        try:
                            body_text = await page.evaluate("() => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim()")
                            cleaned_body = clean_extracted_problem_text(str(body_text or ""))
                            if cleaned_body and len(cleaned_body) > 50 and has_problem_statement_markers(cleaned_body):
                                task.description = cleaned_body[:4000]
                                self._log(f"📝 Re-extracted problem description from body for {task_label} ({len(task.description)} chars)", "success")
                                self._notify()
                        except Exception:
                            pass
            except Exception as e:
                self._log(f"⚠️ Could not re-extract description for {task_label}: {e}", "warn")

        while task.budgets.has_code_repairs():
            attempt_num = len(task.submissions) + 1
            attempt_label = f"(attempt {attempt_num}/{task.budgets.code_repairs + len(task.submissions)})"
            self._log(f"   ▸ {task_label}/{total_tasks} {attempt_label}")

            try:
                success = await self._solve_and_verify(task, total_tasks=total_tasks)
            except Exception as exc:
                task.error_log.append(f"Exception: {exc}")
                self._log(f"   ✖ {task_label} exception: {exc}", "error")
                success = False

            if success:
                task.status = TaskStatus.SUCCESS
                if can_transition(task, TaskState.PASSED):
                    transition(task, TaskState.PASSED)
                else:
                    task.state = TaskState.PASSED
                self._log(f"   ✅ {task_label} — PASSED", "success")
                if hasattr(self, "state") and self.state is not None:
                    self.state.model_thinking = None
                    self.state.model_next_goal = None
                    self.state.model_memory = None
                    self.state.model_last_actions = []
                self._notify()
                return

            # Check if user requested skip
            if self.hitl.skip_requested or task.state == TaskState.SKIPPED:
                task.status = TaskStatus.SKIPPED
                if can_transition(task, TaskState.SKIPPED):
                    transition(task, TaskState.SKIPPED)
                else:
                    task.state = TaskState.SKIPPED
                self._log(f"   ⏭️ {task_label} — SKIPPED", "warn")
                self._notify()
                return

            if task.budgets.has_code_repairs():
                try:
                    task.budgets.use_code_repair()
                except Exception:
                    pass

            if task.budgets.has_code_repairs():
                if can_transition(task, TaskState.REPAIRING):
                    transition(task, TaskState.REPAIRING)
                self._log(f"   🔄 {task_label} — retrying ({task.budgets.code_repairs} repairs left)…", "warn")
                if can_transition(task, TaskState.PLANNING):
                    transition(task, TaskState.PLANNING)
            else:
                self._log(
                    f"   ⚠️ {task_label} exhausted automatic repairs. Pausing for human assistance.",
                    "warn",
                )
                self._set_phase(AgentPhase.PAUSED_HITL)
                self.state.hitl_reason = (
                    f"{task_label} failed automatically. Please check the headed browser, "
                    f"correct any code errors in the editor, and click Resume Agent to retry!"
                )
                self._notify()

                # Pause and wait for human to click Resume
                await self.hitl.trigger_pause(self.state.hitl_reason)

                # Once resumed:
                self._set_phase(AgentPhase.EXECUTING)
                self.state.hitl_reason = None
                self._log(f"▶️ Resumed by user. Replenishing repair budget for {task_label}.", "success")
                task.budgets.code_repairs = 3
                if can_transition(task, TaskState.PLANNING):
                    transition(task, TaskState.PLANNING)
                self._notify()

                # Check suggestions
                suggs = self.control_flags.get("suggestions", []).copy()
                if suggs:
                    self._log(f"💬 Checked suggestions upon resume: Found and active: {suggs}", "success")
                else:
                    self._log("💬 Checked suggestions upon resume: None injected.", "info")

        if not is_terminal(task):
            if can_transition(task, TaskState.FAILED):
                transition(task, TaskState.FAILED)
        self._notify()

    # ------------------------------------------------------------------
    # Frontend task detection + reference image extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_frontend_task(task: CodingTask) -> bool:
        """Detect whether a task is a frontend/webdev assignment based on its content."""
        # If recon already extracted a visual design spec, it's definitely frontend
        if task.visual_design_spec and task.visual_design_spec.strip():
            return True
        # Keyword-based detection in the task description
        desc_lower = task.description.lower()
        frontend_keywords = [
            "html", "css", "frontend", "front-end", "webdev", "web dev",
            "mockup", "design mockup", "reference image", "refer to the below image",
            "refer below image", "layout", "responsive", "bootstrap", "flexbox",
            "grid layout", "styled-component", "ui component", "web page",
            "webpage", "landing page", "navbar", "header", "footer",
            "card component", "button style", "font-family", "background-color",
            "margin", "padding", "border-radius", "box-shadow",
        ]
        return any(kw in desc_lower for kw in frontend_keywords)

    async def _with_overlay_hidden(self, page, async_fn):
        """Temporarily hide __omnitasker_panel before running async_fn, then restore it."""
        if page is None:
            return await async_fn()
        try:
            await page.evaluate("""() => {
                const p = document.getElementById('__omnitasker_panel');
                if (p) p.style.setProperty('display', 'none', 'important');
            }""")
        except Exception:
            pass

        try:
            return await async_fn()
        finally:
            try:
                await page.evaluate("""() => {
                    const p = document.getElementById('__omnitasker_panel');
                    if (p) {
                        p.style.removeProperty('display');
                        if (window.getComputedStyle(p).display === 'none') {
                            p.style.setProperty('display', 'flex', 'important');
                        }
                    }
                }""")
            except Exception:
                pass

    def _cleanup_ref_images(self) -> None:
        """Remove all extracted reference images from disk."""
        import shutil
        import os
        ref_dir = os.path.join("D:\\auto", "ref_images")
        if os.path.exists(ref_dir):
            try:
                shutil.rmtree(ref_dir, ignore_errors=True)
                self._log("🧹 Cleaned up ref_images directory from disk.", "info")
            except Exception as e:
                print(f"Error cleaning ref_images: {e}")

    async def _extract_ref_images_to_disk(self) -> list[dict]:
        """Extract all reference/mockup images from the current page and save them to disk."""
        browser = await self.browser_mgr.get_browser()
        page = await self._get_current_page_safe(browser)
        if page is None:
            self._log("⚠️ No active page available for reference image extraction.", "warn")
            return []

        async def _do_extract():
            return await extract_ref_images_to_disk(browser, page, log_fn=self._log)

        return await self._with_overlay_hidden(page, _do_extract)

    async def _solve_and_verify(self, task: CodingTask, total_tasks: int = 1) -> bool:
        """Run an isolated sub-agent (TaskSubagent) to solve a single task and verify output."""
        self._current_task = task
        subagent = TaskSubagent(
            task=task,
            orchestrator=self,
            perception_engine=self.perception_engine,
        )

        try:
            result = await subagent.execute(total_tasks=total_tasks)
        except asyncio.CancelledError:
            if getattr(self, "_task_completed_successfully", False) or (self._last_report and self._last_report.get("success")):
                task.status = TaskStatus.SUCCESS
                self._code_written_in_current_task = True
                return True
            raise

        if result.skipped:
            self._user_skip_requested = True
            task.status = TaskStatus.SUCCESS
            self._code_written_in_current_task = True
            return True

        if result.success:
            task.status = TaskStatus.SUCCESS
            self._code_written_in_current_task = True
            if result.solution_code:
                task.solution_code = result.solution_code
            return True

        return False

    # ------------------------------------------------------------------
    # Controller & Custom Actions
    # ------------------------------------------------------------------

    def _consume_suggestions_block(self, context_name: str = "", inline: bool = False) -> str:
        """Safely consumes and formats any active human user suggestions from control_flags."""
        return consume_suggestions_block(self, context_name=context_name, inline=inline)

    def _build_controller(self) -> Controller:
        """Create a Controller with custom agent actions."""
        return build_controller(self)

    # ------------------------------------------------------------------
    # In-Browser Overlay Injection & Synchronization
    # ------------------------------------------------------------------

    async def _background_poll_loop(self) -> None:
        """Continuously syncs state and polls user commands from all browser pages."""
        return await self.overlay_mgr.background_poll_loop()

    async def _inject_overlay(self, page) -> None:
        """Read overlay_panel.html and inject it into the target page DOM."""
        return await self.overlay_mgr.inject_overlay(page)

    async def _sync_overlay_state(self, page) -> None:
        """Serialize current AgentState and push it to the hidden DOM element."""
        return await self.overlay_mgr.sync_overlay_state(page)

    async def _poll_overlay_commands(self, page) -> None:
        """Read and dispatch user commands from the overlay's hidden element."""
        return await self.overlay_mgr.poll_overlay_commands(page)

    # ------------------------------------------------------------------
    # HITL Gate
    # ------------------------------------------------------------------

    async def _get_current_page_safe(self, browser=None) -> Any:
        """Get active page safely without opening new tabs or triggering browser.start() timeouts."""
        if browser is None:
            if not hasattr(self, "browser_mgr") or not self.browser_mgr.is_open:
                return None
            try:
                browser = await self.browser_mgr.get_browser()
            except Exception:
                return None

        # Auto-heal using browser.reconnect() if CDP connection was dropped
        if not getattr(browser, "is_cdp_connected", False):
            if hasattr(browser, "reconnect") and callable(getattr(browser, "reconnect", None)):
                try:
                    reconn_res = browser.reconnect()
                    if asyncio.iscoroutine(reconn_res) or hasattr(reconn_res, "__await__"):
                        await reconn_res
                except Exception:
                    pass
            if not getattr(browser, "is_cdp_connected", False):
                return None

        page = None
        try:
            page = await browser.get_current_page()
        except Exception:
            # On connection error during get_current_page, attempt auto-heal via reconnect()
            if hasattr(browser, "reconnect") and callable(getattr(browser, "reconnect", None)):
                try:
                    reconn_res = browser.reconnect()
                    if asyncio.iscoroutine(reconn_res) or hasattr(reconn_res, "__await__"):
                        await reconn_res
                    page = await browser.get_current_page()
                except Exception:
                    page = None
            else:
                page = None

        if page is None:
            try:
                pages = await browser.get_pages()
                if pages:
                    page = pages[0]
            except Exception:
                # If get_pages fails with connection error, attempt auto-heal via reconnect()
                if hasattr(browser, "reconnect") and callable(getattr(browser, "reconnect", None)):
                    try:
                        reconn_res = browser.reconnect()
                        if asyncio.iscoroutine(reconn_res) or hasattr(reconn_res, "__await__"):
                            await reconn_res
                        pages = await browser.get_pages()
                        if pages:
                            page = pages[0]
                    except Exception:
                        page = None
                else:
                    page = None

        return page

    async def _hitl_gate(self, current_url: str) -> None:
        """Evaluate security gate and authentication walls, prompting user if needed."""
        # Immediately disable CDP debugger pauses so Chrome doesn't freeze on login page scripts
        try:
            browser = await self.browser_mgr.get_browser()
            page = await self._get_current_page_safe(browser)
            if page is not None:
                await self._disable_cdp_debugger_pauses(browser, page)
                # Enable DevTools CDP perception domains
                await self.perception_engine.enable_cdp_domains(browser, page)
        except Exception:
            pass

        # Evaluate through Two-Layer Security Gate (Phase 2)
        sec_res = self.security_gate.evaluate_url(
            current_url=current_url,
            target_url=self.url,
            is_hitl_paused=self.hitl.is_paused,
        )

        if sec_res.decision == SecurityDecision.DENIED:
            reason = f"Security Gate: Blocked navigation to prohibited URL: {sec_res.reason}"
            self._log(f"🛑 {reason}", "error")
            self._set_phase(AgentPhase.PAUSED_HITL)
            self.state.hitl_reason = reason
            self._notify()
            await self.hitl.trigger_pause(reason)
            self._set_phase(AgentPhase.EXECUTING)
            self.state.hitl_reason = None
            self._notify()
            return

        if sec_res.decision == SecurityDecision.NEEDS_APPROVAL:
            reason = f"Security Gate: Unknown external domain '{sec_res.domain}' detected. User approval required."
            self._log(f"⚠️ {reason}", "warn")
            self._set_phase(AgentPhase.PAUSED_HITL)
            self.state.hitl_reason = reason
            self._notify()
            await self.hitl.trigger_pause(reason)
            # Upon user resuming/approving:
            self.security_gate.add_allowed_domain(sec_res.domain, persistent=False)
            self._set_phase(AgentPhase.EXECUTING)
            self.state.hitl_reason = None
            self._notify()
            self._log(f"✅ Domain '{sec_res.domain}' approved by user for this session.", "success")
            return

        # Check authentication walls
        if self.hitl._url_is_auth(current_url):
            reason = f"Authentication wall detected: {current_url}"
            self._log(f"⏸️ {reason}", "warn")
            self._set_phase(AgentPhase.PAUSED_HITL)
            self.state.hitl_reason = reason
            self._notify()
            await self.hitl.check_and_wait(current_url)
            self._log("▶️ Resumed by user. Waiting for page to stabilize...", "success")
            try:
                browser = await self.browser_mgr.get_browser()
                page = await self._get_current_page_safe(browser)
                if page is not None:
                    await asyncio.sleep(5)  # Wait for SPA editors to load
            except Exception as e:
                self._log(f"Stability wait warning: {e}", "warn")
            self._set_phase(AgentPhase.EXECUTING)
            self.state.hitl_reason = None
            self._notify()
            self._log("▶️ Resumed and stabilized.", "success")

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _is_allowed_domain(self, target_url: str, current_url: str) -> bool:
        """Return True if current_url is permitted by the two-layer security gate."""
        return self.security_gate.is_url_allowed(
            current_url=current_url,
            target_url=target_url,
            is_hitl_paused=getattr(self.hitl, "is_paused", False),
        )

    async def _disable_cdp_debugger_pauses(self, browser, page) -> None:
        """Tell Chrome CDP to skip all debugger; pauses and disable breakpoints on the page session."""
        if browser is None or page is None:
            return
        try:
            cdp = getattr(browser, "_cdp_client_root", None)
            if cdp and hasattr(cdp, "send_raw"):
                sid = await page.session_id
                if sid:
                    await cdp.send_raw("Debugger.enable", {}, session_id=sid)
                    await cdp.send_raw("Debugger.setSkipAllPauses", {"skip": True}, session_id=sid)
                    await cdp.send_raw("Debugger.setBreakpointsActive", {"active": False}, session_id=sid)
                    await cdp.send_raw("Page.addScriptToEvaluateOnNewDocument", {
                        "source": """
                            try {
                                window.Function.prototype.constructor = function() { return function() {}; };
                            } catch(e) {}
                        """
                    }, session_id=sid)
        except Exception:
            pass

    async def _on_agent_step(self, browser_state: Any, model_output: Any, step_number: int = 0) -> None:
        """Callback invoked by browser-use after each step."""
        import time

        # Fast exit if submission already verified passing
        if getattr(self, "_task_completed_successfully", False) or (self._last_report and self._last_report.get("success")):
            if hasattr(self, "state") and self.state is not None:
                self.state.model_thinking = None
            task_id_str = str(self._current_task.task_id) if self._current_task else ""
            self._log(f"🏁 Task #{task_id_str} submission verified passing! Advancing to next task.", "success")
            raise asyncio.CancelledError("Task completed successfully.")

        # 1. Prevent unauthorized domain redirection / navigate away (ad redirects, popups, etc.)
        # Skip if HITL is currently paused (e.g. user is logging in or interacting manually)
        if not (getattr(self, "hitl", None) and self.hitl.is_paused):
            try:
                browser = await self.browser_mgr.get_browser()
                pages = await browser.get_pages()

                if len(pages) > 1:
                    active_page = await self._get_current_page_safe(browser)
                    pages_to_close = []
                    for p in pages:
                        p_url = await p.get_url()
                        # Close extra about:blank, chrome://newtab/, or unauthorized external tabs to release CDP debugger locks
                        if p != active_page and (not p_url or p_url in ("about:blank", "chrome://newtab/") or not self._is_allowed_domain(self.url, p_url)):
                            pages_to_close.append(p)
                    
                    # Close any extra pages/tabs immediately
                    for p in pages_to_close:
                        p_url = await p.get_url()
                        self._log(f"⚠️ Closing extra tab to release debugger lock: {p_url}", "warn")
                        try:
                            res = browser.close_page(p)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception:
                            pass
                
                # Check and update active URL safely without forced page.goto redirection
                page = await self._get_current_page_safe(browser)
                if page is not None:
                    await self._disable_cdp_debugger_pauses(browser, page)
                    current_url = await page.get_url()
                    if current_url and current_url not in ("about:blank", "chrome://newtab/"):
                        if self._is_allowed_domain(self.url, current_url):
                            self.url = current_url  # Update tracking URL to prevent stale redirect loops
            except Exception as e:
                print(f"Exception in domain redirect safeguard: {e}")

        # Intercept mid-run skip suggestions from overlay
        if self.control_flags and self.control_flags.get("suggestions"):
            suggs = self.control_flags["suggestions"].copy()
            skip_keywords = ["skip", "next task", "go to next", "completed", "done", "ignore", "intro"]
            for s in suggs:
                if any(kw in s.lower() for kw in skip_keywords):
                    self._log(f"⏭️ User typed skip directive ('{s}') mid-run. Automatically completing current task!", "success")
                    self._code_written_in_current_task = True
                    self._user_skip_requested = True
                    self.control_flags["suggestions"].clear()
                    raise asyncio.CancelledError("Skipped by user suggestion.")

        # 2. Check for Stop Request
        if self.control_flags.get("stop_requested") or self._stop_requested:
            self._log("🛑 Stop signal received mid-step. Terminating agent loop.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        # 2. Check for Graceful Mid-Step Pause Request
        if self.control_flags.get("pause_requested"):
            click_time = self.control_flags.get("pause_click_time", time.time())
            elapsed = time.time() - click_time
            self.control_flags["pause_elapsed_time"] = elapsed
            self.control_flags["pause_requested"] = False  # Consume request

            self._log(f"⏸️ Pause initiated mid-step. Agent gracefully paused in {elapsed:.2f} seconds after clicking pause.", "warn")
            original_phase = self.state.phase
            self._set_phase(AgentPhase.PAUSED_HITL)
            self.state.hitl_reason = f"Agent gracefully paused mid-step by user. Elapsed: {elapsed:.2f} seconds."
            self._notify()

            # Block execution at the current step until Resume button is clicked
            await self.hitl.trigger_pause(self.state.hitl_reason)

            # Once resumed
            self._set_phase(original_phase)
            self.state.hitl_reason = None
            self.control_flags["pause_elapsed_time"] = None
            self._log("▶️ Resuming agent loop. Checking for human suggestions...", "info")
            self._notify()

            # Immediately check suggestions
            suggs = self.control_flags.get("suggestions", []).copy()
            if suggs:
                self._log(f"💬 Checked suggestions upon resume: Found and active: {suggs}", "success")
            else:
                self._log("💬 Checked suggestions upon resume: None injected.", "info")

        if model_output and hasattr(self, "state") and self.state is not None:
            if getattr(self, "_task_completed_successfully", False) or (self._last_report and self._last_report.get("success")):
                self.state.model_thinking = None
            else:
                self.state.model_thinking = getattr(model_output, "thinking", None)
            self.state.model_memory = getattr(model_output, "memory", None)
            self.state.model_next_goal = getattr(model_output, "next_goal", None)
            
            actions = []
            try:
                for act in getattr(model_output, "action", []):
                    try:
                        act_dict = act.model_dump(exclude_none=True)
                        for k in act_dict.keys():
                            actions.append(k)
                    except Exception:
                        actions.append(str(act))
            except Exception:
                pass
            self.state.model_last_actions = actions
            self._notify()

        # 4. Overlay state sync & command polling on each step
        try:
            if self.browser_mgr.is_open:
                browser = await self.browser_mgr.get_browser()
                page = await self._get_current_page_safe(browser)
                if page is not None:
                    p_url = await page.get_url()
                    if p_url and not p_url.startswith("chrome://") and not p_url.startswith("devtools://") and not p_url.startswith("edge://") and p_url != "about:blank":
                        res = await page.evaluate("() => (!!document.getElementById('__omnitasker_panel') && window.__omnitasker_js_initialized === true)")
                        overlay_ready = (str(res).lower() == "true" or res is True)
                        if not overlay_ready:
                            await self._inject_overlay(page)
                        else:
                            await self._sync_overlay_state(page)
                        await self._poll_overlay_commands(page)
        except Exception:
            pass

    async def _safe_sync_overlay(self) -> None:
        """Safely sync overlay state to the active browser page."""
        return await self.overlay_mgr.safe_sync_overlay()

    def _set_phase(self, phase: AgentPhase) -> None:
        self.state.phase = phase
        self._notify()
        try:
            # Schedule an async state sync with the overlay panel immediately
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._safe_sync_overlay())
        except Exception:
            pass

    async def _inject_code_direct(self, browser, code: str) -> Optional[str]:
        """Directly inject solution code into Monaco, CodeMirror, Ace, Contenteditable, or Textarea editors on the active page."""
        page = await self._get_current_page_safe(browser)
        if page is None:
            return None
        injected = await inject_code_to_editor(page, code)
        if injected:
            self._code_written_in_current_task = True
            self.state.current_solution_code = None
            self.state.failed_injection_code = None
            self._notify()
        return injected

    def _log(self, msg: str, level: str = "info") -> None:
        if getattr(self, "_task_completed_successfully", False):
            msg_lower = msg.lower()
            if any(term in msg_lower for term in ["thinking", "thought", "reasoning", "model thought"]):
                return
        if self.state.logs and self.state.logs[-1].message == msg and self.state.logs[-1].level == level:
            return
        entry = _make_log(msg, level)
        self.state.logs.append(entry)
        self._notify()

    def _notify(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change(self.state)
            except Exception:
                pass  # Never let a UI callback crash the agent.
