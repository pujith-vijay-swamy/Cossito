"""
platform_adapters.py — Platform-specific result parsing and task identity detection.

Defines adapters for coding platforms (generic fallback with progressive DOM detection).
Parses test results, detects terminal execution states, and extracts task identity.

Schema version: 2.0
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from models import PlatformTask, SubmissionRecord
from verification import parse_test_results

logger = logging.getLogger(__name__)


class PlatformAdapter(ABC):
    """Abstract base class for platform-specific adapters."""

    @abstractmethod
    async def detect(self, page: Any) -> bool:
        """Returns True if this platform is detected on the page."""
        pass

    @abstractmethod
    async def is_terminal(self, page: Any) -> bool:
        """Returns True if results are final (not still evaluating/pending)."""
        pass

    @abstractmethod
    async def parse_result(self, page: Any) -> SubmissionRecord:
        """Parse current test results from the page into a SubmissionRecord."""
        pass

    @abstractmethod
    async def get_current_task_identity(self, page: Any) -> PlatformTask:
        """Detect which task/question is currently selected on the page."""
        pass

    @abstractmethod
    async def click_submit(self, page: Any) -> bool:
        """Click the submit button."""
        pass

    @abstractmethod
    async def click_run(self, page: Any) -> bool:
        """Click the run/test button."""
        pass


class GenericAdapter(PlatformAdapter):
    """Progressive detection adapter that handles any standard coding platform."""

    async def detect(self, page: Any) -> bool:
        return True

    async def is_terminal(self, page: Any) -> bool:
        try:
            res = await page.evaluate(r"""() => {
                // 1. Check for active loading spinners
                const spinners = document.querySelectorAll(
                    '.mantine-Loader-root, [class*="loader" i], [class*="spinner" i], [aria-busy="true"], svg[class*="animate-spin" i]'
                );
                for (const sp of spinners) {
                    // Editor loading, unrelated widgets, and the agent overlay
                    // must not keep a completed submission in the polling loop.
                    if (sp.closest('#__omnitasker_panel')) continue;
                    const resultArea = sp.closest('[data-cy="submission-result"], .submission-result, .test-results, [class*="testResults" i], [class*="submissionResults" i], #output, .output-panel, .console-output, .terminal, [class*="testcase" i], [role="status"]');
                    const control = sp.closest('button, [role="button"]');
                    const submissionControl = control && /submit|run|judg|compil/i.test(control.innerText || control.getAttribute('aria-label') || '');
                    if (sp.offsetParent !== null && (resultArea || submissionControl)) return "evaluating";
                }
                // 2. Check disabled submit button with loading indicator
                const submitBtns = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const btn of submitBtns) {
                    if (btn.closest('#__omnitasker_panel') || btn.offsetParent === null) continue;
                    const txt = (btn.innerText || '').toLowerCase();
                    if (txt.includes('submit') || txt.includes('running') || txt.includes('judging')) {
                        if ((btn.hasAttribute('data-loading') && btn.getAttribute('data-loading') !== 'false') || btn.classList.contains('mantine-Button-loading') || btn.getAttribute('aria-busy') === 'true') {
                            return "evaluating";
                        }
                    }
                }
                // 3. Check dedicated output and result containers (exclude problem statement tab)
                const dedicatedSelectors = [
                    '[data-cy="submission-result"]', '.submission-result', '.test-results',
                    '[class*="testResults" i]', '[class*="submissionResults" i]',
                    '.mantine-Alert-root', '[role="alert"]', '.mantine-Notification-root',
                    '.mantine-Modal-body', '[role="dialog"]', '.mantine-Table-root',
                    '#output', '.output-panel', '.console-output', '.terminal', '[class*="testcase" i]'
                ];
                for (const sel of dedicatedSelectors) {
                    const el = document.querySelector(sel);
                    if (el && (el.innerText || '').trim().length > 3) {
                        return (el.innerText || '').trim();
                    }
                }
                return document.body ? (document.body.innerText || '') : '';
            }""")
            text = str(res) if res is not None else ""
            text_lower = text.lower()

            # If unambiguous terminal result is present, it is terminal
            if re.search(r'\b(accepted|all\s+test\s*cases?\s+passed|all\s+tests?\s+passed|congratulations|wrong\s+answer|time\s+limit\s+exceeded|runtime\s+error|compilation\s+error|correct\s+answer|status:\s*success|submission\s+successful|100%\s+passed|100%\s+test\s*cases?\s+passed)\b', text_lower):
                return True
            if re.search(r'\b\d+\s*(?:/|out of|of)\s*\d+\s*(?:test\s*cases?|testcases?|tests?)?\s*passed\b', text_lower):
                return True
            if re.search(r'\b(?:passed|test\s*cases?\s*passed|tests?\s*passed)\s*:?\s*\d+\s*(?:/|out of|of)\s*\d+\b', text_lower):
                return True
            if re.search(r'\b(?:score|points?|marks?)\s*:?\s*\d+\s*(?:/|out of|of)\s*\d+\b', text_lower):
                return True
            if re.search(r'passed:\s*\d+,\s*failed:\s*\d+', text_lower):
                return True
            if re.search(r'\b\d+\s*passed\s*[,;/]?\s*\d+\s*failed\b', text_lower):
                return True

            pending_patterns = [
                r'\bevaluating\b',
                r'\bsubmitting\b',
                r'\brunning\s+(?:test\s*cases?|tests?|code)\b',
                r'\bplease\s+wait\b',
                r'\bpending\b',
                r'\bjudging\b',
                r'\bcompiling\b',
                r'\bin\s+progress\b',
            ]
            if text == "evaluating":
                return False
            if any(re.search(p, text_lower) for p in pending_patterns):
                return False
            return True
        except Exception:
            return True

    async def parse_result(self, page: Any) -> SubmissionRecord:
        try:
            res = await page.evaluate(r"""() => {
                const visible = el => el && !el.closest('#__omnitasker_panel, [hidden], [aria-hidden="true"]') && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
                // 1. Dedicated test results containers / alerts / modals
                const dedicatedSelectors = [
                    '[data-cy="submission-result"]', '.submission-result', '.test-results',
                    '.result-container', '[class*="testResults" i]', '[class*="submissionResults" i]',
                    '[class*="submission-result" i]', '[class*="submission-status" i]',
                    '.mantine-Alert-root', '[role="alert"]',
                    '.mantine-Notification-root', '[class*="notification" i]',
                    '.mantine-Modal-body', '[role="dialog"]'
                ];
                for (const sel of dedicatedSelectors) {
                    const el = Array.from(document.querySelectorAll(sel)).find(visible);
                    if (el && (el.innerText || '').trim().length > 3) {
                        const txt = (el.innerText || '').trim();
                        if (/passed|failed|accepted|wrong|error|test\s*case|score|verdict/i.test(txt)) {
                            return txt;
                        }
                    }
                }

                // 2. Active Tab panel (EXCLUDE problem description / statement)
                const tabPanels = Array.from(document.querySelectorAll('.mantine-Tabs-panel[data-active="true"], [role="tabpanel"]:not([hidden])'));
                for (const panel of tabPanels) {
                    if (!visible(panel)) continue;
                    const panelId = (panel.getAttribute('id') || panel.getAttribute('aria-labelledby') || panel.getAttribute('data-value') || '').toLowerCase();
                    if (panelId.includes('submission') || panelId.includes('history') || panelId.includes('desc') || panelId.includes('prob') || panelId.includes('stat') || panelId.includes('detail')) {
                        continue;
                    }
                    const pTxt = (panel.innerText || '').trim();
                    if (pTxt.length > 5 && /passed|failed|accepted|wrong|error|test\s*case|score|runtime/i.test(pTxt)) {
                        return pTxt;
                    }
                }

                // 5. Output / console / terminal
                const outputSelectors = ['#output', '.output-panel', '.console-output', '.terminal', '[class*="output" i]', '[class*="testcase" i]', '[class*="test-case" i]'];
                for (const sel of outputSelectors) {
                    const el = Array.from(document.querySelectorAll(sel)).find(visible);
                    if (el && (el.innerText || '').trim().length > 3) {
                        return (el.innerText || '').trim();
                    }
                }

                // The problem statement, history tables, and overlay logs can all
                // contain "Accepted". They are not the current submission result.
                return '';
            }""")
            text = str(res) if res is not None else ""
            record = parse_test_results(text)
            record.terminal = await self.is_terminal(page)
            if record.outcome in (None, "unknown") and record.passed_count is None:
                record.terminal = False
            return record
        except Exception as e:
            return SubmissionRecord(
                terminal=False,
                outcome="unknown",
                raw_output=f"Error parsing result: {e}",
            )

    async def get_current_task_identity(self, page: Any) -> PlatformTask:
        try:
            data = await page.evaluate("""() => {
                const title = document.title || '';
                const url = window.location.href || '';
                // Try to find active question tab
                const activeTab = document.querySelector('.active, [aria-selected="true"], .selected');
                const tabText = activeTab ? (activeTab.innerText || '').trim() : '';
                return { title, url, tabText };
            }""")
            from dom_extraction import parse_eval_result
            data = parse_eval_result(data)
            if not isinstance(data, dict):
                data = {}
            return PlatformTask(
                key=data.get("tabText") or data.get("title") or "1",
                title=data.get("tabText") or data.get("title") or "Question 1",
                url=data.get("url") or "",
            )
        except Exception:
            return PlatformTask(key="1", title="Question 1", url="")

    async def click_submit(self, page: Any) -> bool:
        try:
            clicked = await page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
                for (const btn of buttons) {
                    const txt = (btn.innerText || btn.value || '').trim().toLowerCase();
                    if (txt.includes('feedback') || txt.includes('bug') || txt.includes('doubt') || txt.includes('ticket')) {
                        continue;
                    }
                    if (txt === 'submit' || txt === 'submit code' || txt === 'submit solution' || txt.includes('submit')) {
                        if (btn.offsetParent !== null && !btn.disabled) {
                            try {
                                btn.scrollIntoView({ block: 'nearest' });
                                btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                                btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                                btn.click();
                            } catch(e) {
                                btn.click();
                            }
                            return true;
                        }
                    }
                }
                return false;
            }""")
            from dom_extraction import parse_eval_result
            return parse_eval_result(clicked) is True
        except Exception:
            return False

    async def click_run(self, page: Any) -> bool:
        try:
            clicked = await page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const btn of buttons) {
                    const txt = (btn.innerText || '').trim().toLowerCase();
                    if (txt === 'run' || txt === 'run code' || txt === 'test' || txt === 'run tests') {
                        if (btn.offsetParent !== null && !btn.disabled) {
                            try {
                                btn.scrollIntoView({ block: 'nearest' });
                                btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                                btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                                btn.click();
                            } catch(e) {
                                btn.click();
                            }
                            return true;
                        }
                    }
                }
                return false;
            }""")
            from dom_extraction import parse_eval_result
            return parse_eval_result(clicked) is True
        except Exception:
            return False


async def detect_platform(page: Any) -> PlatformAdapter:
    """Try specific platform adapters, falling back to GenericAdapter."""
    return GenericAdapter()


async def wait_for_terminal(
    page: Any,
    adapter: PlatformAdapter,
    timeout: float = 30.0,
    poll_interval: float = 1.0,
    baseline: Optional[SubmissionRecord] = None,
) -> SubmissionRecord:
    """Wait within one deadline, including browser reads; never reparse after it."""
    record = SubmissionRecord(terminal=False, outcome="unknown")
    if timeout <= 0:
        return record

    async def poll():
        nonlocal record
        observed_pending = False
        # Give the platform a chance to replace the previous verdict on submit.
        await asyncio.sleep(min(0.5, timeout * 0.1))
        while True:
            # parse_result already checks terminal state. Avoid a redundant DOM
            # round trip before each parse.
            record = await adapter.parse_result(page)
            if not record.terminal:
                observed_pending = True
            if record.terminal and (
                record.outcome in ("accepted", "wrong_answer", "time_limit_exceeded", "runtime_error", "compilation_error")
                or record.passed_count is not None
            ) and (baseline is None or observed_pending or result_fingerprint(record) != result_fingerprint(baseline)):
                return record
            await asyncio.sleep(max(0.01, poll_interval))

    try:
        return await asyncio.wait_for(poll(), timeout=timeout)
    except asyncio.TimeoutError:
        record.terminal = False
        return record


def result_fingerprint(record: SubmissionRecord) -> tuple:
    """Compare observable verdicts, excluding locally generated IDs/timestamps."""
    return (record.outcome, record.passed_count, record.total_count, record.failed_count,
            ' '.join(record.raw_output.split()))
