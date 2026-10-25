"""
human_loop.py — Human-in-the-Loop pause/resume controller (v2).

Provides typed control commands, durable pause records, scoped guidance,
and asyncio-based signaling. The asyncio.Event remains as a process-local
wake-up mechanism; the PauseRecord is the durable source of truth.

Schema version: 2.0
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from models import ControlCommand, PauseRecord


# URL patterns that indicate a login or auth wall.
_AUTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/login", re.IGNORECASE),
    re.compile(r"/auth", re.IGNORECASE),
    re.compile(r"/signin", re.IGNORECASE),
    re.compile(r"/sign-in", re.IGNORECASE),
    re.compile(r"/sso", re.IGNORECASE),
    re.compile(r"/oauth", re.IGNORECASE),
    re.compile(r"/captcha", re.IGNORECASE),
]


class HumanInTheLoopController:
    """Manages pause/resume signaling between the async agent loop and the UI.

    v2 improvements over v1:
    - Typed ControlCommand enum instead of bare strings
    - Durable PauseRecord as source of truth (not just asyncio.Event)
    - Scoped guidance attached to specific tasks
    - Skip semantics (record skipped, don't mark as success)
    - Cancel semantics (reconcile in-flight actions)

    The controller exposes two surfaces:
    1. **Agent-side** — ``check_and_wait(url)`` is called by the agent before
       each action step. If the URL matches an auth pattern or the controller
       was explicitly paused, it blocks until ``resume()`` is called.
    2. **UI-side** — ``pause(reason)``, ``resume()``, ``skip()``, ``cancel()``,
       and ``add_guidance()`` are called from the Streamlit thread.
    """

    def __init__(self) -> None:
        self._resume_event: Optional[asyncio.Event] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self.is_paused: bool = False
        self.pause_reason: str = ""

        # v2 additions
        self.pause_records: list[PauseRecord] = []
        self.pending_commands: list[ControlCommand] = []
        self.guidance: dict[str, list[str]] = {}  # task_id → guidance strings
        self._skip_requested: bool = False
        self._cancel_requested: bool = False

    def _ensure_event(self) -> asyncio.Event:
        """Lazily create the Event on the running loop."""
        if self._resume_event is None:
            self._event_loop = asyncio.get_running_loop()
            self._resume_event = asyncio.Event()
            self._resume_event.set()  # start unpaused
        return self._resume_event

    def _signal_resume(self) -> None:
        """Wake the agent safely when controls run on the Streamlit thread."""
        if self._resume_event is not None and self._event_loop is not None:
            if not self._event_loop.is_closed():
                self._event_loop.call_soon_threadsafe(self._resume_event.set)

    # ------------------------------------------------------------------
    # Agent-side API
    # ------------------------------------------------------------------

    async def check_and_wait(self, current_url: str) -> None:
        """Called by the agent before each step.

        If the current URL matches a login/auth pattern, or the controller
        was explicitly paused, this coroutine blocks until ``resume()`` is
        called from the UI.
        """
        evt = self._ensure_event()

        if not self.is_paused and self._url_is_auth(current_url):
            self.pause(f"Authentication wall detected: {current_url}")

        if self.is_paused:
            evt.clear()
            await evt.wait()

    async def trigger_pause(self, reason: str) -> None:
        """Called by the agent's custom `pause_for_human` action.

        This allows the LLM to programmatically pause the loop when it
        visually detects a CAPTCHA or login form that wasn't caught by URL
        pattern matching.
        """
        self.pause(reason)
        evt = self._ensure_event()
        evt.clear()
        await evt.wait()

    def has_pending_command(self, command: ControlCommand) -> bool:
        """Check if a specific command is pending."""
        return command in self.pending_commands

    def consume_command(self, command: ControlCommand) -> bool:
        """Consume (remove) a pending command. Returns True if it was present."""
        if command in self.pending_commands:
            self.pending_commands.remove(command)
            return True
        return False

    def get_guidance(self, task_id: str) -> list[str]:
        """Get guidance strings for a specific task (non-destructive read)."""
        return self.guidance.get(task_id, [])

    def consume_guidance(self, task_id: str) -> list[str]:
        """Get and clear guidance for a specific task."""
        return self.guidance.pop(task_id, [])

    @property
    def skip_requested(self) -> bool:
        return self._skip_requested

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    # ------------------------------------------------------------------
    # UI-side API (called from Streamlit thread)
    # ------------------------------------------------------------------

    def pause(self, reason: str, task_id: Optional[str] = None) -> None:
        """Pause the agent loop with a reason string."""
        self.is_paused = True
        self.pause_reason = reason
        self.pause_records.append(
            PauseRecord(reason=reason, task_id=task_id, command=ControlCommand.PAUSE)
        )
        if self._resume_event is not None:
            self._resume_event.clear()

    def resume(self) -> None:
        """Resume the agent loop.

        v2: re-observation and state verification should happen
        in the orchestrator after this returns, not here.
        """
        self.is_paused = False
        self.pause_reason = ""
        self._skip_requested = False
        self._cancel_requested = False
        self.pending_commands.append(ControlCommand.RESUME)
        self._signal_resume()

    def skip(self, task_id: Optional[str] = None) -> None:
        """Request the current task be skipped.

        The orchestrator should record 'skipped_by_user', retain the last
        result, and advance. Skip is NOT success.
        """
        self._skip_requested = True
        self.pending_commands.append(ControlCommand.SKIP)
        if task_id:
            self.pause_records.append(
                PauseRecord(
                    reason=f"User requested skip for task {task_id}",
                    task_id=task_id,
                    command=ControlCommand.SKIP,
                )
            )
        # Also resume so the agent loop unblocks
        self.is_paused = False
        self.pause_reason = ""
        self._signal_resume()

    def cancel(self) -> None:
        """Request cancellation of the current run.

        The orchestrator should reconcile in-flight actions before
        terminating.
        """
        self._cancel_requested = True
        self.pending_commands.append(ControlCommand.CANCEL)
        self.is_paused = False
        self.pause_reason = ""
        self._signal_resume()

    def add_guidance(self, task_id: str, guidance: str) -> None:
        """Attach guidance to a specific task.

        Guidance is scoped to a task — it doesn't mutate agent state
        from an unvalidated string.
        """
        if task_id not in self.guidance:
            self.guidance[task_id] = []
        self.guidance[task_id].append(guidance)

    def approve_submit(self, task_id: Optional[str] = None) -> None:
        """Approve submission for a task (when manual approval is enabled)."""
        self.pending_commands.append(ControlCommand.APPROVE_SUBMIT)
        if task_id:
            self.pause_records.append(
                PauseRecord(
                    reason=f"Submission approved for task {task_id}",
                    task_id=task_id,
                    command=ControlCommand.APPROVE_SUBMIT,
                )
            )
        # Unblock if paused waiting for approval
        if self.is_paused:
            self.is_paused = False
            self.pause_reason = ""
            self._signal_resume()

    # ------------------------------------------------------------------
    # URL matching
    # ------------------------------------------------------------------

    def _url_is_auth(self, url: str) -> bool:
        """Return True if *url* looks like a login / auth page."""
        if not url:
            return False
        return any(p.search(url) for p in _AUTH_PATTERNS)
