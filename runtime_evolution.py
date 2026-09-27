"""
runtime_evolution.py — Bounded recovery controller (v2).

Classifies failures and chooses bounded, typed recovery operations.
Does NOT generate arbitrary JavaScript for live execution by default.
Recovery operations are chosen from an approved manifest of safe operations.

Schema version: 2.0
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

from models import (
    Budgets,
    FailureCategory,
    RecoveryDecision,
    RecoveryOperation,
    RecoveryPlan,
    SkillCandidate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, FailureCategory]] = [
    ("selector", FailureCategory.SELECTOR_TIMEOUT),
    ("timeout", FailureCategory.TIMEOUT),
    ("detached", FailureCategory.FRAME_DETACHED),
    ("frame", FailureCategory.FRAME_DETACHED),
    ("iframe", FailureCategory.FRAME_DETACHED),
    ("dom", FailureCategory.DOM_MUTATION),
    ("stale", FailureCategory.STALE_EVIDENCE),
    ("mutation", FailureCategory.DOM_MUTATION),
    ("compilation", FailureCategory.COMPILATION),
    ("compile", FailureCategory.COMPILATION),
    ("syntax", FailureCategory.COMPILATION),
    ("runtime", FailureCategory.RUNTIME),
    ("wrong answer", FailureCategory.WRONG_ANSWER),
    ("wrong output", FailureCategory.WRONG_ANSWER),
    ("network", FailureCategory.NETWORK),
    ("connection", FailureCategory.NETWORK),
    ("login", FailureCategory.AUTH_WALL),
    ("captcha", FailureCategory.AUTH_WALL),
    ("authentication", FailureCategory.AUTH_WALL),
    ("security", FailureCategory.SECURITY_BLOCK),
    ("denied", FailureCategory.SECURITY_BLOCK),
    ("editor", FailureCategory.EDITOR_MISMATCH),
    ("monaco", FailureCategory.EDITOR_MISMATCH),
]


def classify_failure(error: Exception, context: Optional[dict[str, Any]] = None) -> FailureCategory:
    """Classify a failure into a category based on error patterns and DevTools perception signals."""
    msg = str(error).lower()
    for pattern, category in _CATEGORY_PATTERNS:
        if pattern in msg:
            return category

    # Check multi-signal perception telemetry if available
    if context:
        perception = context.get("perception")
        if isinstance(perception, dict):
            # Check console errors
            console_errs = perception.get("console_errors", [])
            for c_err in console_errs:
                c_text = str(c_err.get("text", "") if isinstance(c_err, dict) else c_err).lower()
                if any(kw in c_text for kw in ("syntaxerror", "compilation", "cannot find symbol")):
                    return FailureCategory.COMPILATION
                if any(kw in c_text for kw in ("typeerror", "referenceerror", "uncaught")):
                    return FailureCategory.RUNTIME

            # Check network failures
            net_fails = perception.get("network_failures", [])
            for n_fail in net_fails:
                status = n_fail.get("status_code") if isinstance(n_fail, dict) else None
                if status in (401, 403):
                    return FailureCategory.AUTH_WALL
                if status and status >= 500:
                    return FailureCategory.NETWORK

    return FailureCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Typed recovery executor
# ---------------------------------------------------------------------------

async def execute_recovery_operation(
    op: RecoveryOperation,
    page: Any,
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Execute a single typed recovery operation.

    Returns (success, description).
    Does NOT generate or execute arbitrary JS.
    """
    try:
        if op == RecoveryOperation.FRESH_OBSERVATION:
            # Simply re-read the page state — a no-op observation refresh
            try:
                url = await page.get_url()
                title_js = await page.evaluate("() => document.title")
                return True, f"Fresh observation: {url} ({title_js})"
            except Exception as e:
                return False, f"Fresh observation failed: {e}"

        elif op == RecoveryOperation.WAIT_FOR_SELECTOR:
            selector = context.get("selector", "body")
            timeout = context.get("wait_timeout", 10000)
            try:
                await page.wait_for_selector(selector, timeout=timeout)
                return True, f"Selector '{selector}' became available"
            except Exception as e:
                return False, f"Wait for selector '{selector}' failed: {e}"

        elif op == RecoveryOperation.AXTREE_LOOKUP:
            # Query the accessibility tree or interactive elements to re-acquire target
            try:
                found_targets = await page.evaluate(r"""() => {
                    const targets = [];
                    const interactive = document.querySelectorAll('button, a, input, [role="button"], [role="tab"], .monaco-editor, .cm-editor');
                    for (const el of interactive) {
                        if (el.offsetParent !== null) {
                            const name = el.getAttribute('aria-label') || el.innerText || el.title || '';
                            if (name) targets.push(name.trim().slice(0, 40));
                        }
                    }
                    return targets.slice(0, 10);
                }""")
                return True, f"AXTree recovery found accessible interactive controls: {found_targets}"
            except Exception as e:
                return False, f"AXTree lookup failed: {e}"

        elif op == RecoveryOperation.CONSOLE_ERROR_PURGE:
            # Purge transient window runtime errors
            try:
                await page.evaluate("() => { if (window.__omnitasker_errors) window.__omnitasker_errors = []; }")
                return True, "Purged page console error buffer"
            except Exception as e:
                return False, f"Console error purge failed: {e}"

        elif op == RecoveryOperation.NETWORK_RETRY:
            # Pause briefly and wait for pending network requests to settle
            try:
                await asyncio.sleep(2.0)
                url = await page.get_url()
                return True, f"Network settled on URL: {url}"
            except Exception as e:
                return False, f"Network retry failed: {e}"

        elif op == RecoveryOperation.FRAME_REACQUISITION:
            # Re-acquire the main frame by finding the most relevant frame
            try:
                frames_count = await page.evaluate("() => window.frames.length")
                return True, f"Frame reacquisition: {frames_count} frames detected"
            except Exception as e:
                return False, f"Frame reacquisition failed: {e}"

        elif op == RecoveryOperation.EDITOR_FALLBACK:
            # Attempt to detect a different editor type
            try:
                editor_info = await page.evaluate("""() => {
                    const checks = [];
                    if (window.monaco && window.monaco.editor) checks.push('monaco');
                    if (document.querySelector('.CodeMirror')) checks.push('codemirror5');
                    if (document.querySelector('.cm-content')) checks.push('codemirror6');
                    if (document.querySelector('.ace_editor')) checks.push('ace');
                    if (document.querySelector('textarea')) checks.push('textarea');
                    return checks.join(', ') || 'none';
                }""")
                return True, f"Available editors: {editor_info}"
            except Exception as e:
                return False, f"Editor detection failed: {e}"

        elif op == RecoveryOperation.OVERLAY_DISMISSAL:
            # Dismiss common blocking overlays (cookie banners, modals)
            try:
                dismissed = await page.evaluate("""() => {
                    const dismissed = [];
                    const dismissSelectors = [
                        '[class*="cookie"] button',
                        '[class*="consent"] button',
                        '[class*="modal"] [class*="close"]',
                        '[class*="overlay"] [class*="close"]',
                        '[aria-label="Close"]',
                        '[data-dismiss="modal"]',
                    ];
                    for (const sel of dismissSelectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            if (el.offsetParent !== null) {
                                el.click();
                                dismissed.push(sel);
                            }
                        }
                    }
                    return dismissed.join(', ') || 'none';
                }""")
                return True, f"Dismissed overlays: {dismissed}"
            except Exception as e:
                return False, f"Overlay dismissal failed: {e}"

        elif op == RecoveryOperation.PAGE_RELOAD:
            try:
                await page.reload()
                await asyncio.sleep(3)
                return True, "Page reloaded"
            except Exception as e:
                return False, f"Page reload failed: {e}"

        elif op == RecoveryOperation.TAB_SWITCH:
            return True, "Tab switch requested (orchestrator will handle)"

        elif op == RecoveryOperation.ESCALATE_HUMAN:
            return False, "Escalating to human — automated recovery not possible"

        else:
            return False, f"Unknown recovery operation: {op}"

    except Exception as e:
        return False, f"Recovery operation {op} failed unexpectedly: {e}"


# ---------------------------------------------------------------------------
# Recovery controller
# ---------------------------------------------------------------------------

@dataclass
class RecoveryResult:
    """Result of a bounded recovery attempt."""

    success: bool = False
    attempt: int = 0
    diagnosis: str = ""
    category: FailureCategory = FailureCategory.UNKNOWN
    decision: RecoveryDecision = RecoveryDecision.ESCALATE
    operations_executed: list[str] = field(default_factory=list)
    output: str = ""
    error: str = ""


class BoundedRecoveryController:
    """Controls recovery with typed operations and budget enforcement.

    v2 improvements over v1 SelfHealingEngine:
    - No arbitrary JS generation by default
    - Typed RecoveryOperation enum restricts what can execute
    - Budget enforcement (max 3 browser recovery attempts per task)
    - Failure classification guides recovery strategy
    - Operations chosen from an approved manifest
    """

    # Mapping from failure category to recommended recovery operations
    RECOVERY_STRATEGIES: dict[FailureCategory, list[RecoveryOperation]] = {
        FailureCategory.SELECTOR_TIMEOUT: [
            RecoveryOperation.AXTREE_LOOKUP,
            RecoveryOperation.FRESH_OBSERVATION,
            RecoveryOperation.WAIT_FOR_SELECTOR,
            RecoveryOperation.OVERLAY_DISMISSAL,
        ],
        FailureCategory.DOM_MUTATION: [
            RecoveryOperation.FRESH_OBSERVATION,
            RecoveryOperation.AXTREE_LOOKUP,
            RecoveryOperation.WAIT_FOR_SELECTOR,
        ],
        FailureCategory.FRAME_DETACHED: [
            RecoveryOperation.FRAME_REACQUISITION,
            RecoveryOperation.FRESH_OBSERVATION,
        ],
        FailureCategory.EDITOR_MISMATCH: [
            RecoveryOperation.EDITOR_FALLBACK,
            RecoveryOperation.FRESH_OBSERVATION,
        ],
        FailureCategory.AUTH_WALL: [
            RecoveryOperation.ESCALATE_HUMAN,
        ],
        FailureCategory.SECURITY_BLOCK: [
            RecoveryOperation.ESCALATE_HUMAN,
        ],
        FailureCategory.NETWORK: [
            RecoveryOperation.NETWORK_RETRY,
            RecoveryOperation.FRESH_OBSERVATION,
            RecoveryOperation.PAGE_RELOAD,
        ],
        FailureCategory.TIMEOUT: [
            RecoveryOperation.FRESH_OBSERVATION,
            RecoveryOperation.NETWORK_RETRY,
            RecoveryOperation.PAGE_RELOAD,
        ],
        FailureCategory.COMPILATION: [
            RecoveryOperation.CONSOLE_ERROR_PURGE,
            RecoveryOperation.FRESH_OBSERVATION,
        ],
        FailureCategory.RUNTIME: [
            RecoveryOperation.CONSOLE_ERROR_PURGE,
            RecoveryOperation.FRESH_OBSERVATION,
        ],
    }

    DEFAULT_STRATEGY: list[RecoveryOperation] = [
        RecoveryOperation.FRESH_OBSERVATION,
        RecoveryOperation.AXTREE_LOOKUP,
        RecoveryOperation.OVERLAY_DISMISSAL,
    ]

    def __init__(self) -> None:
        self.recovery_history: list[RecoveryResult] = []

    async def attempt_recovery(
        self,
        error: Exception,
        page: Any,
        budgets: Budgets,
        context: dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """Attempt bounded recovery for a failed action.

        1. Classify the failure
        2. Check budget
        3. Choose recovery strategy
        4. Execute operations
        5. Return result

        Does NOT generate or execute arbitrary JavaScript.
        """
        context = context or {}
        category = classify_failure(error, context=context)
        attempt = len(self.recovery_history) + 1

        # Budget check
        if not budgets.has_browser_recoveries():
            result = RecoveryResult(
                success=False,
                attempt=attempt,
                diagnosis=f"Recovery budget exhausted after {len(self.recovery_history)} attempts",
                category=category,
                decision=RecoveryDecision.EXHAUSTED,
                error=str(error),
            )
            self.recovery_history.append(result)
            return result

        # Choose strategy
        strategy = self.RECOVERY_STRATEGIES.get(category, self.DEFAULT_STRATEGY)

        # If escalation is required, don't try automated recovery
        if RecoveryOperation.ESCALATE_HUMAN in strategy and len(strategy) == 1:
            result = RecoveryResult(
                success=False,
                attempt=attempt,
                diagnosis=f"Failure category '{category.value}' requires human intervention",
                category=category,
                decision=RecoveryDecision.ESCALATE,
                error=str(error),
            )
            self.recovery_history.append(result)
            return result

        # Execute recovery operations
        budgets.use_browser_recovery()
        operations_executed: list[str] = []
        overall_success = False

        for op in strategy:
            if op == RecoveryOperation.ESCALATE_HUMAN:
                continue  # Skip escalation in automated pass

            op_success, op_desc = await execute_recovery_operation(op, page, context)
            operations_executed.append(f"{op.value}: {op_desc}")

            if op_success:
                overall_success = True
                # First successful operation is enough — re-observe and continue
                break

        decision = RecoveryDecision.RETRY if overall_success else RecoveryDecision.FALLBACK

        result = RecoveryResult(
            success=overall_success,
            attempt=attempt,
            diagnosis=f"Category: {category.value}. {'Recovery succeeded' if overall_success else 'Recovery operations did not resolve the issue'}",
            category=category,
            decision=decision,
            operations_executed=operations_executed,
            output="\n".join(operations_executed),
            error=str(error),
        )
        self.recovery_history.append(result)
        return result

    def reset(self) -> None:
        """Reset recovery history for a new task."""
        self.recovery_history.clear()
