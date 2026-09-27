"""
recon_engine.py — Reconnaissance pipeline for OmniTasker.

Extracts the reconnaissance phase logic: navigation, DOM hydration,
problem extraction, section scanning, and task queue construction.
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from typing import Any, Optional

from browser_use import Agent, Controller
from llm_provider import get_llm
from prompts import get_recon_system_prompt, get_recon_task_prompt
import sys

def _get_sym(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("agent_core")
    return getattr(mod, name, fallback) if mod is not None else fallback
from models import CodingTask, PlatformTask, TaskQueue, TaskState, TaskStatus
from dom_extraction import (
    is_section_header_text as _is_section_header_text,
    is_navigation_or_overview_text as _is_navigation_or_overview_text,
    clean_extracted_problem_text,
    extract_problem_title_from_text,
    has_problem_statement_markers,
    parse_eval_result,
)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from LLM output."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        from llm_provider import repair_json
        repaired = repair_json(text)
        return json.loads(repaired)
    except Exception:
        pass
    import re as _re
    m = _re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, _re.DOTALL)
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
    m = _re.search(r'\{.*\}', text, _re.DOTALL)
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


class ReconEngine:
    """Handles Phase 1: Reconnaissance.
    
    Orchestrates navigation, DOM hydration, problem extraction,
    and task queue construction.
    """

    def __init__(self, orchestrator):
        """Accept the orchestrator instance for state/browser/logging access."""
        self._orch = orchestrator

    def _build_tasks_from_candidates(
        self,
        candidates: list[dict],
        extracted_text: str,
        initial_design: str = "",
    ) -> list[CodingTask]:
        """Build CodingTask list from section question candidates, binding extracted_text to the active question."""
        tasks: list[CodingTask] = []
        clean_desc = clean_extracted_problem_text(extracted_text)
        valid_desc = clean_desc if (len(clean_desc) > 30 and not _is_navigation_or_overview_text(clean_desc)) else ""
        inferred_title = (extract_problem_title_from_text(valid_desc) or "").lower().strip()

        active_idx = -1
        for i, q in enumerate(candidates):
            if q.get("active"):
                active_idx = i
                break
        if active_idx == -1 and inferred_title:
            for i, q in enumerate(candidates):
                q_t = (q.get("text") or "").lower().strip()
                if q_t and (q_t == inferred_title or inferred_title in q_t or q_t in inferred_title):
                    active_idx = i
                    break
        if active_idx == -1:
            for i, q in enumerate(candidates):
                if not q.get("completed"):
                    active_idx = i
                    break
        if active_idx == -1:
            active_idx = 0

        for i, q in enumerate(candidates):
            q_text = q.get("text", f"Question {i+1}")
            if len(candidates) == 1 and _is_section_header_text(q_text):
                q_text = extract_problem_title_from_text(valid_desc) or "Question 1"
            t = CodingTask(queue_index=i, platform_task=PlatformTask(key=str(i + 1), title=q_text))
            if i == active_idx and valid_desc:
                t.description = f"{q_text}. {valid_desc}"
                t.visual_design_spec = initial_design
            else:
                t.description = f"{q_text}. Solve the coding problem for {q_text} in the section."
                t.visual_design_spec = ""
            t.expected_behavior = "100% test cases pass (0 failed)"
            if q.get("completed"):
                t.status = TaskStatus.SUCCESS
                t.state = TaskState.PASSED
            tasks.append(t)
        return tasks

    async def _defensively_recover_dom_tq(self, browser, current_url: str) -> Optional[TaskQueue]:
        """Defensively recover problem statement and tasks directly from DOM after an agent/LLM error."""
        try:
            page = await self._orch._get_current_page_safe(browser)
            if page is None:
                return None
            extracted_text, section_questions, _ = await self._orch._extract_problem_from_dom_direct(page)
            extracted_text = clean_extracted_problem_text(extracted_text)
            if not extracted_text or len(extracted_text) < 30:
                try:
                    body_text = await page.evaluate("() => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim()")
                    cleaned_body = clean_extracted_problem_text(str(body_text or ""))
                    if cleaned_body and len(cleaned_body) > 30:
                        extracted_text = cleaned_body[:4000]
                except Exception:
                    pass

            detected = section_questions or getattr(self._orch, "_detected_section_questions", [])
            real_qs = [q for q in detected if not _is_section_header_text(q.get("text", ""))]
            candidates = real_qs if real_qs else detected

            if len(candidates) > 1:
                tasks = self._build_tasks_from_candidates(candidates, extracted_text)
                return TaskQueue(tasks=tasks, source_url=current_url, recon_summary="Defensive DOM recovery: Section tasks recovered directly from page DOM")
            elif len(candidates) == 1:
                tasks = self._build_tasks_from_candidates(candidates, extracted_text)
                return TaskQueue(tasks=tasks, source_url=current_url, recon_summary="Defensive DOM recovery: Question statement recovered directly from page DOM")
            elif len(extracted_text) >= 15:
                title = extract_problem_title_from_text(extracted_text) or "Question 1"
                t = CodingTask(queue_index=0, platform_task=PlatformTask(key="1", title=title))
                t.description = extracted_text
                t.expected_behavior = "100% test cases pass (0 failed)"
                return TaskQueue(tasks=[t], source_url=current_url, recon_summary="Defensive DOM recovery: Problem statement recovered directly from page DOM")
            else:
                title = "Question 1"
                try:
                    raw_title = await page.evaluate("() => document.title || ''")
                    if raw_title:
                        title = str(raw_title).split('-')[0].split('|')[0].strip() or "Question 1"
                except Exception:
                    pass
                desc = extracted_text or f"Solve the coding problem for {title}."
                t = CodingTask(queue_index=0, platform_task=PlatformTask(key="1", title=title))
                t.description = desc
                t.expected_behavior = "100% test cases pass (0 failed)"
                return TaskQueue(tasks=[t], source_url=current_url, recon_summary="Defensive DOM recovery: Recovered from page context")
        except Exception as e:
            self._orch._log(f"Defensive DOM recovery note: {e}", "warn")
        return None



    async def _run_recon(self) -> Optional[TaskQueue]:
        """Navigate to the URL and produce a TaskQueue."""
        self._orch._log(f"🔍 Recon: navigating to {self._orch.url}")

        # Phase 2 Security Gate: Enforce 2-Layer URL check BEFORE sending any packets to target URL
        await self._orch._hitl_gate(self._orch.url)

        browser = await self._orch.browser_mgr.get_browser()
        if not getattr(browser, "is_cdp_connected", False):
            try:
                await browser.start()
            except Exception as e:
                self._orch._log(f"Browser startup note: {e}", "warn")

        current_url = self._orch.url
        page = None
        if getattr(browser, "is_cdp_connected", False):
            try:
                page = await self._orch._get_current_page_safe(browser)
                p_url = None
                if page is not None:
                    try:
                        p_url = await page.get_url()
                    except Exception:
                        p_url = None
                target_url = self._orch.url or ""
                target_base = target_url.split('?')[0] if target_url else ""
                has_target_params = "?" in target_url
                p_url_base = p_url.split('?')[0] if p_url else ""

                needs_nav = (
                    not p_url
                    or p_url in ("about:blank", "chrome://newtab/")
                    or (target_base and not p_url.startswith(target_base))
                    or (has_target_params and ("?" not in p_url or p_url != target_url))
                )
                if needs_nav:
                    self._orch._log(f"🔗 Navigating to target URL: {self._orch.url}", "info")
                    nav_done = False
                    if hasattr(browser, "navigate_to") and callable(getattr(browser, "navigate_to", None)):
                        try:
                            nav_res = browser.navigate_to(self._orch.url)
                            if asyncio.iscoroutine(nav_res) or hasattr(nav_res, "__await__"):
                                await nav_res
                            nav_done = True
                        except Exception:
                            nav_done = False
                    if not nav_done and page is not None and hasattr(page, "goto"):
                        await page.goto(self._orch.url)
                    await asyncio.sleep(3)  # Wait for page hydration

                # Re-acquire page after navigation
                page = await self._orch._get_current_page_safe(browser)
                if page is not None:
                    await self._orch._disable_cdp_debugger_pauses(browser, page)
                    await self._orch._inject_overlay(page)
                    await self._orch._sync_overlay_state(page)
                    current_url = await page.get_url()
            except Exception as e:
                self._orch._log(f"Pre-navigation warning: {e}", "warn")
                current_url = self._orch.url
                try:
                    if getattr(browser, "is_cdp_connected", False):
                        page = await self._orch._get_current_page_safe(browser)
                except Exception:
                    page = None

        self._orch._log(f"🔍 Target URL: {current_url}")
        await self._orch._hitl_gate(current_url)

        # ⚡ Direct Fast-Track Reconnaissance Engine (Instant 1.5s vs 60-90s)
        if page is not None:
            try:
                # Progressive hydration poll: single-page applications (MentorPick, LeetCode) take 2-4s to load markdown/tabs
                extracted_text = ""
                section_questions = []
                embedded_images = []
                for poll_i in range(6):
                    try:
                        extracted_text, section_questions, embedded_images = await self._orch._extract_problem_from_dom_direct(page)
                        self._orch._detected_section_questions = section_questions
                    except Exception as poll_err:
                        await asyncio.sleep(0.8)
                        try:
                            page = await self._orch._get_current_page_safe(browser)
                        except Exception:
                            pass
                        continue

                    extracted_text = clean_extracted_problem_text(extracted_text)

                    real_qs = [q for q in section_questions if not _is_section_header_text(q.get("text", ""))]
                    if (real_qs and len(extracted_text) >= 50) or (real_qs and "?active=" not in current_url) or (not real_qs and len(extracted_text) >= 50):
                        break
                    await asyncio.sleep(0.8)

                real_candidates = [q for q in section_questions if not _is_section_header_text(q.get("text", ""))]
                candidates = real_candidates if real_candidates else section_questions
                pending_detected = [q for q in candidates if not q.get("completed")]

                # If only 1 candidate was found and its title is a section header, or 0 candidates, expand it and re-scan
                is_section_header = (
                    len(candidates) == 1 and _is_section_header_text(candidates[0].get("text", ""))
                )
                if is_section_header or len(candidates) == 0:
                    header_txt = candidates[0].get("text", "") if candidates else ""
                    self._orch._log(f"🧭 Checking for expandable section headers ({header_txt or 'Practice Problems'})...", "info")
                    try:
                        expanded_any = await page.evaluate("""(txt) => {
                            let clicked = false;
                            const btns = Array.from(document.querySelectorAll(
                                '.mantine-Accordion-control, button[class*="Accordion-control"], [data-accordion-control], ' +
                                '.mantine-NavLink-root, button[class*="NavLink"], a[class*="NavLink"], [aria-expanded]'
                            ));
                            for (const b of btns) {
                                const bTxt = (b.innerText || '').toLowerCase().trim();
                                const matches = txt ? bTxt.includes(txt.toLowerCase()) : /(practice|problem|exercise|assignment|chapter|module|section|week|topic)/i.test(bTxt);
                                if (matches) {
                                    const isExpanded = b.getAttribute('aria-expanded') === 'true' || b.getAttribute('data-expanded') === 'true' || b.classList.contains('mantine-Accordion-control-opened') || b.classList.contains('mantine-NavLink-opened');
                                    if (!isExpanded) {
                                        try {
                                            b.scrollIntoView({ block: 'nearest' });
                                            b.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                                            b.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                                            b.click();
                                            clicked = true;
                                        } catch(e) {}
                                    }
                                }
                            }
                            return clicked;
                        }""", header_txt)
                        if parse_eval_result(expanded_any) is True:
                            await asyncio.sleep(1.0)
                            extracted_text, new_section_questions, embedded_images = await self._orch._extract_problem_from_dom_direct(page)
                            extracted_text = clean_extracted_problem_text(extracted_text)
                            if new_section_questions:
                                section_questions = new_section_questions
                                self._orch._detected_section_questions = section_questions
                                new_real = [q for q in section_questions if not _is_section_header_text(q.get("text", ""))]
                                candidates = new_real if new_real else section_questions
                                pending_detected = [q for q in candidates if not q.get("completed")]
                    except Exception:
                        pass

                # If problem description is missing (< 20 chars), matches navigation/overview text, or multi-question section needs hydration (< 50 chars):
                # auto-click the target question to hydrate the problem statement
                is_bad_desc = len(extracted_text) < 20 or _is_navigation_or_overview_text(extracted_text)
                should_hydrate = bool(candidates) and (is_bad_desc or (len(candidates) > 1 and len(extracted_text) < 50))
                if should_hydrate:
                    active_match = re.search(r'active=([^&#]+)', current_url)
                    active_key = active_match.group(1).lower() if active_match else ""
                    target_q = pending_detected[0] if pending_detected else candidates[0]
                    target_q_text = target_q.get("text", "")
                    self._orch._log(f"🧭 Problem statement unhydrated ({len(extracted_text)} chars). Hydrating via question: '{target_q_text}'...", "info")
                    eval_coro = page.evaluate("""(activeId, targetTitle) => {
                        const targetLower = (targetTitle || '').toLowerCase().trim();

                        function expandParents(el) {
                            try {
                                let curr = el ? el.parentElement : null;
                                while (curr && curr !== document.body) {
                                    const acc = curr.closest && curr.closest('.mantine-Accordion-item, [class*="Accordion-item"]');
                                    if (acc) {
                                        const control = acc.querySelector('.mantine-Accordion-control, button[class*="Accordion-control"]');
                                        if (control) {
                                            const isOpened = control.getAttribute('aria-expanded') === 'true' || control.getAttribute('data-opened') === 'true' || control.classList.contains('mantine-Accordion-control-opened');
                                            if (!isOpened) {
                                                try { control.click(); } catch(e) {}
                                            }
                                        }
                                    }
                                    const parentNavLink = curr.closest && curr.closest('.mantine-NavLink-root, [class*="NavLink"]');
                                    if (parentNavLink && parentNavLink !== el) {
                                        const isOpened = parentNavLink.getAttribute('data-opened') === 'true' || parentNavLink.getAttribute('aria-expanded') === 'true' || parentNavLink.classList.contains('mantine-NavLink-opened');
                                        if (!isOpened) {
                                            try { parentNavLink.click(); } catch(e) {}
                                        }
                                    }
                                    curr = curr.parentElement;
                                }
                            } catch(err) {}
                        }

                        // 1. If activeId present in URL, click link containing activeId
                        if (activeId) {
                            const activeLink = document.querySelector(`a[href*="${activeId}"], [data-id*="${activeId}"], [data-active-id*="${activeId}"]`);
                            if (activeLink) {
                                expandParents(activeLink);
                                activeLink.click();
                                return "Clicked active question matching URL ID";
                            }
                        }
                        // 2. Click question matching targetTitle
                        const candidateLinks = Array.from(document.querySelectorAll(
                            '.mantine-NavLink-children .mantine-NavLink-root, .mantine-NavLink-children a, .mantine-NavLink-root, button[class*="NavLink"], a[href*="chapter"], a[href*="active="], [class*="question"]'
                        ));
                        for (const el of candidateLinks) {
                            if (el.querySelector('.mantine-NavLink-chevron, [data-chevron], [class*="chevron" i], [class*="caret" i], [class*="arrow" i], [class*="Accordion-chevron"]') ||
                                el.getAttribute('aria-expanded') === 'true' ||
                                el.getAttribute('data-opened') === 'true' ||
                                el.querySelector('.mantine-NavLink-root, button[class*="NavLink"]')) {
                                continue;
                            }
                            const labelEl = el.querySelector('.mantine-NavLink-label, [class*="label"]') || el;
                            const txt = (labelEl.innerText || el.innerText || '').toLowerCase().trim();
                            if (targetLower && (txt === targetLower || (txt.length >= 3 && targetLower.includes(txt)) || (targetLower.length >= 3 && txt.includes(targetLower)))) {
                                expandParents(el);
                                el.click();
                                return "Clicked question matching title: " + (labelEl.innerText || el.innerText || '').trim();
                            }
                        }
                        const fallbackLink = document.querySelector('.mantine-NavLink-children a, .mantine-NavLink-children .mantine-NavLink-root, .mantine-NavLink-children button, a[href*="active="], [class*="sidebar"] a');
                        if (fallbackLink) {
                            expandParents(fallbackLink);
                            fallbackLink.click();
                            return "Clicked first child question link";
                        }
                        return null;
                    }""", active_key, target_q_text)
                    click_res = await eval_coro if (asyncio.iscoroutine(eval_coro) or hasattr(eval_coro, "__await__")) else eval_coro
                    click_res = parse_eval_result(click_res)

                    if click_res:
                        self._orch._log(f"🧭 {click_res}. Waiting for problem statement to hydrate...", "info")
                        await asyncio.sleep(1.5)
                        try:
                            current_url = await page.get_url()
                        except Exception:
                            pass
                        for re_poll in range(4):
                            extracted_text, new_section_questions, embedded_images = await self._orch._extract_problem_from_dom_direct(page)
                            extracted_text = clean_extracted_problem_text(extracted_text)
                            if len(extracted_text) >= 50:
                                break
                            await asyncio.sleep(0.8)
                        if new_section_questions:
                            section_questions = new_section_questions
                            self._orch._detected_section_questions = section_questions
                            candidates = section_questions
                            pending_detected = [q for q in section_questions if not q.get("completed")]

                if len(candidates) > 1:
                    self._orch._log(f"⚡ Fast Recon: Section scanner detected {len(candidates)} question(s) directly in DOM!", "success")
                    tasks = self._build_tasks_from_candidates(candidates, extracted_text)
                    return TaskQueue(
                        tasks=tasks,
                        source_url=current_url,
                        recon_summary=f"Fast Recon: {len(tasks)} tasks catalogued from section navigation",
                    )
                elif len(candidates) == 1:
                    tasks = self._build_tasks_from_candidates(candidates, extracted_text)
                    task_title = tasks[0].platform_task.title
                    self._orch._log(f"⚡ Fast Recon: Section scanner detected 1 question ('{task_title}') directly in DOM ({len(extracted_text)} chars)!", "success")
                    return TaskQueue(
                        tasks=tasks,
                        source_url=current_url,
                        recon_summary="Fast Recon: Problem statement extracted directly from DOM",
                    )
                elif len(extracted_text) > 50 and not _is_navigation_or_overview_text(extracted_text):
                    task_title = extract_problem_title_from_text(extracted_text) or "Question 1"
                    self._orch._log(f"⚡ Fast Recon: Problem statement ('{task_title}') extracted directly from DOM ({len(extracted_text)} chars) in ~1s!", "success")
                    task = CodingTask(
                        queue_index=0,
                        platform_task=PlatformTask(key="1", title=task_title),
                    )
                    task.description = extracted_text
                    task.expected_behavior = "100% test cases pass (0 failed)"
                    return TaskQueue(
                        tasks=[task],
                        source_url=current_url,
                        recon_summary="Fast Recon: Problem statement extracted directly from DOM",
                    )
            except Exception as e:
                self._orch._log(f"Direct fast-recon note: {e}. Falling back to autonomous recon agent.", "warn")

        # Quick DOM Body Fallback: If body has readable coding problem text, catalog it immediately in 0.2s
        if page is not None:
            try:
                body_text = await page.evaluate("""() => {
                    const b = document.body ? document.body.innerText : '';
                    return (b || '').replace(/\\s+/g, ' ').trim();
                }""")
                cleaned_body = clean_extracted_problem_text(str(body_text or ""))
                if cleaned_body and len(cleaned_body) > 50 and has_problem_statement_markers(cleaned_body):
                    title = extract_problem_title_from_text(cleaned_body) or "Question 1"
                    self._orch._log(f"⚡ Fast Recon: Extracted coding problem ('{title}') directly from page content ({len(cleaned_body)} chars)!", "success")
                    task = CodingTask(
                        queue_index=0,
                        platform_task=PlatformTask(key="1", title=title),
                    )
                    task.description = cleaned_body[:4000]
                    task.expected_behavior = "100% test cases pass (0 failed)"
                    return TaskQueue(
                        tasks=[task],
                        source_url=current_url,
                        recon_summary="Fast Recon: Problem statement recovered directly from page content",
                    )
            except Exception:
                pass

        self._orch._log("🤖 Launching autonomous recon agent as fallback...", "info")

        model = getattr(self._orch, "model", "gemini-2.5-flash")
        api_key = getattr(self._orch, "api_key", None)
        provider = getattr(self._orch, "provider", None)
        base_url = getattr(self._orch, "base_url", None)
        llm = _get_sym("get_llm", get_llm)(model=model, api_key=api_key, provider=provider, base_url=base_url)

        # Build a controller with the pause_for_human custom action.
        controller = self._orch._build_controller()

        use_vision = getattr(self._orch, "use_vision", True)
        agent = _get_sym("Agent", Agent)(
            task=get_recon_task_prompt(self._orch.url, use_vision),
            llm=llm,
            browser=browser,
            controller=controller,
            extend_system_message=get_recon_system_prompt(use_vision),
            max_actions_per_step=3,
            step_timeout=120,
            register_new_step_callback=self._orch._on_agent_step,
            use_vision=use_vision,
        )

        try:
            self._orch._current_agent_task = asyncio.create_task(agent.run(max_steps=10))
            history = await self._orch._current_agent_task
        except asyncio.CancelledError:
            self._orch._log("🛑 Recon agent task cancelled.", "warn")
            raise
        except Exception as run_err:
            err_msg = str(run_err)
            self._orch._log(f"⚠️ Autonomous recon agent error: {err_msg}. Attempting defensive DOM recovery...", "warn")
            recovered_tq = await self.defensively_recover_dom_tq(browser, current_url)
            if recovered_tq:
                self._orch._log("⚡ Defensively recovered problem statement directly from DOM after agent exception!", "success")
                return recovered_tq
            raise
        finally:
            self._orch._current_agent_task = None

        # Check for errors in history (e.g. rate limits or provider failures)
        if history.has_errors():
            non_none_errors = [e for e in history.errors() if e is not None]
            if non_none_errors:
                last_error = str(non_none_errors[-1])
                is_max_steps = "maximum steps" in last_error.lower()
                is_timeout_or_503 = any(kw in last_error.lower() for kw in ["503", "unavailable", "timeout", "timed out", "deadline", "rate limit", "quota"])

                if is_max_steps:
                    self._orch._log("ℹ️ Recon agent reached step limit. Extracting problem and tasks from accumulated history...", "info")
                elif is_timeout_or_503 or ("pause_for_human" not in last_error):
                    self._orch._log(f"⚠️ Recon agent encountered error ({last_error[:100]}...). Attempting defensive DOM recovery...", "warn")
                    recovered_tq = await self.defensively_recover_dom_tq(browser, current_url)
                    if recovered_tq:
                        self._orch._log("⚡ Defensively recovered problem statement directly from DOM after agent error!", "success")
                        return recovered_tq

                if not is_max_steps:
                    # Intercept local model connection failures gracefully
                    if "Connection error" in last_error or "APIConnectionError" in last_error or "failed to connect" in last_error.lower():
                        raise Exception(
                            f"Connection Error: Failed to connect to your LLM provider ('{self._orch.provider}') at '{self._orch.base_url or 'default endpoint'}'.\n\n"
                            f"👉 RESOLUTION STEPS:\n"
                            f"1. If running Ollama locally, make sure your Ollama service is active. Try executing: `ollama run {self._orch.model}` in your terminal to verify.\n"
                            f"2. Ensure that the Ollama port (default: 11434) is accessible. Verify that the URL you entered in the sidebar matches your runner's endpoint (usually 'http://localhost:11434/v1').\n"
                            f"3. If using LM Studio or vLLM, verify that the Local Server is started on the correct port (usually 1234 or 8000) and the model is fully loaded.\n"
                            f"4. If using an online provider, check your internet connection."
                        )
                    # If the error was a human pause timeout where user has since resumed or finished logging in, recover gracefully
                    if "pause_for_human" in last_error and ("timed out" in last_error or "CDP WebSocket" in last_error):
                        self._orch._log("ℹ️ Human login/pause period concluded. Continuing automatically with DOM extraction...", "info")
                    else:
                        recovered_tq = await self.defensively_recover_dom_tq(browser, current_url)
                        if recovered_tq:
                            return recovered_tq
                        raise Exception(f"Reconnaissance failed due to agent errors:\n{last_error}")

        # ---- Extract the TaskQueue from agent history ----
        raw_content = history.final_result() or ""
        self._orch._log(f"🔍 Recon raw output length: {len(raw_content)} chars")

        # If the raw final result is empty, try extracted content.
        if not raw_content:
            extracted = history.extracted_content()
            if extracted:
                raw_content = "\n".join(extracted)

        # Inspect step results (e.g. from extract_problem_description) if raw_content still empty
        if not raw_content and hasattr(history, "history"):
            for step in getattr(history, "history", []):
                for action_res in getattr(step, "result", []) or []:
                    content = getattr(action_res, "extracted_content", "") or getattr(action_res, "error", "")
                    if content and len(str(content)) > len(raw_content):
                        raw_content = str(content)

        parsed = _extract_json(raw_content)
        if parsed is None:
            self._orch._log("⚠️ Failed to parse recon JSON — scanning history extracted content...", "warn")
            for entry in history.extracted_content() or []:
                parsed = _extract_json(entry)
                if parsed:
                    break

        if parsed is None or not isinstance(parsed, dict):
            self._orch._log("⚠️ Recon LLM produced non-JSON output. Executing DOM Fallback TaskQueue generation...", "warn")
            parsed = {"recon_summary": "Extracted problem statement directly from page DOM", "tasks": []}

        # Build TaskQueue from parsed JSON.
        tasks: list[CodingTask] = []
        parsed_tasks = parsed.get("tasks", [])

        # Check if DOM scanner detected questions in a section sidebar
        detected = getattr(self._orch, "_detected_section_questions", [])
        real_qs = [q for q in detected if not _is_section_header_text(q.get("text", ""))]
        candidates = real_qs if real_qs else detected

        if len(candidates) > 1 or (len(candidates) > 0 and len(parsed_tasks) <= 1):
            self._orch._log(f"⚡ Multi-task auto-expansion: Section scanner detected {len(candidates)} questions on the page! Creating task queue for ALL {len(candidates)} tasks.", "success")
            raw_initial_desc = parsed_tasks[0].get("description", "Coding challenge") if parsed_tasks else "Coding challenge"
            initial_desc = clean_extracted_problem_text(raw_initial_desc) or "Coding challenge"
            initial_design = parsed_tasks[0].get("visual_design_spec", "") if parsed_tasks else ""
            tasks = self._build_tasks_from_candidates(candidates, initial_desc, initial_design)
        elif parsed_tasks:
            for i, item in enumerate(parsed_tasks):
                task = CodingTask.from_v1_dict(item, queue_index=i)
                tasks.append(task)
        else:
            # Fallback: create single task from raw DOM extraction text if no section list found
            self._orch._log("⚡ DOM Fallback: Creating Task #1 directly from page problem statement.", "info")
            cleaned_raw = clean_extracted_problem_text(raw_content)
            title = extract_problem_title_from_text(cleaned_raw) or "Question 1"
            task = CodingTask(
                queue_index=0,
                platform_task=PlatformTask(key="1", title=title),
            )
            valid_raw = cleaned_raw[:5000] if len(cleaned_raw) > 30 else "Solve the coding challenge on the page."
            task.description = valid_raw
            task.expected_behavior = "100% test cases pass (0 failed)"
            tasks.append(task)

        return TaskQueue(
            tasks=tasks,
            source_url=self._orch.url,
            recon_summary=parsed.get("recon_summary", "Page Reconnaissance Complete"),
        )

    async def navigate_to_task_question(self, task: CodingTask, total_tasks: int) -> None:
        """Programmatically attempt to switch to the current question/tab on the page if multi-tasking."""
        if total_tasks <= 1:
            return
        try:
            browser = await self._orch.browser_mgr.get_browser()
            page = await self._orch._get_current_page_safe(browser)
            if page is None:
                return

            task_key = task.platform_task.key or str(task.queue_index + 1)
            task_title = (task.platform_task.title or "").strip()

            nav_script = r"""
            (args) => {
                const taskKey = typeof args === 'object' && args !== null ? args.taskKey : args;
                const taskTitle = typeof args === 'object' && args !== null && args.taskTitle ? args.taskTitle.toLowerCase().trim() : '';

                function expandParents(el) {
                    try {
                        let curr = el ? el.parentElement : null;
                        while (curr && curr !== document.body) {
                            const acc = curr.closest && curr.closest('.mantine-Accordion-item, [class*="Accordion-item"]');
                            if (acc) {
                                const control = acc.querySelector('.mantine-Accordion-control, button[class*="Accordion-control"]');
                                const panel = acc.querySelector('.mantine-Accordion-panel');
                                const isOpened = control && (
                                    control.getAttribute('aria-expanded') === 'true' ||
                                    control.getAttribute('data-opened') === 'true' ||
                                    control.classList.contains('mantine-Accordion-control-opened') ||
                                    (panel && panel.offsetHeight > 0 && !panel.hidden && panel.style.display !== 'none')
                                );
                                if (control && !isOpened) {
                                    try { control.click(); } catch(e) {}
                                }
                            }
                            if (curr.classList && (curr.classList.contains('mantine-NavLink-children') || curr.classList.contains('mantine-Accordion-panel') || curr.classList.contains('mantine-Collapse-root'))) {
                                const prev = curr.previousElementSibling;
                                if (prev) {
                                    const isOpened = prev.getAttribute('data-opened') === 'true' ||
                                                     prev.getAttribute('data-expanded') === 'true' ||
                                                     prev.getAttribute('aria-expanded') === 'true' ||
                                                     prev.classList.contains('mantine-NavLink-opened') ||
                                                     (curr.offsetHeight > 0 && curr.style.display !== 'none' && curr.style.height !== '0px');
                                    if (!isOpened) {
                                        try { prev.click(); } catch(e) {}
                                    }
                                }
                            }
                            curr = curr.parentElement;
                        }
                    } catch(err) {}
                }

                const candidateSelectors = [
                    '.mantine-NavLink-children .mantine-NavLink-root',
                    '.mantine-NavLink-children a',
                    '.mantine-NavLink-children button',
                    '.mantine-Collapse-root .mantine-NavLink-root',
                    '.mantine-Collapse-root a',
                    '.mantine-Collapse-root button',
                    '.mantine-Accordion-panel .mantine-NavLink-root',
                    '.mantine-Accordion-panel a',
                    '.mantine-Accordion-panel button',
                    '.mantine-NavLink-root', 'button[class*="NavLink"]',
                    'a[href*="chapter"]', 'a[href*="active="]',
                    '[class*="question-item"]', '[class*="problem-item"]',
                    '[role="tab"]', '.question-tab', 'button', 'a'
                ];
                const allLinks = Array.from(document.querySelectorAll(candidateSelectors.join(',')));

                // 1. Support matching by task.platform_task.title (e.g. Mantine NavLinks on MentorPick)
                if (taskTitle && taskTitle.length > 1) {
                    for (const el of allLinks) {
                        if (el.querySelector('.mantine-NavLink-chevron, [data-chevron], [class*="chevron"], [class*="rightSection"] svg') ||
                            el.getAttribute('aria-expanded') === 'true' ||
                            el.getAttribute('data-opened') === 'true' ||
                            el.querySelector('.mantine-NavLink-root, button[class*="NavLink"]')) {
                            continue;
                        }
                        const labelEl = el.querySelector('.mantine-NavLink-label, [class*="label"]') || el;
                        const txt = (labelEl.innerText || el.innerText || '').toLowerCase().trim().replace(/\s+/g, ' ');
                        const minLen = Math.min(6, Math.floor(taskTitle.length * 0.5));
                        if (txt && (txt === taskTitle || (txt.length >= minLen && (taskTitle.includes(txt) || txt.includes(taskTitle))))) {
                            expandParents(el);
                            try {
                                el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                                el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                                el.click();
                            } catch(e) {
                                el.click();
                            }
                            if (el.tagName.toLowerCase() !== 'a') {
                                const innerA = el.querySelector('a');
                                if (innerA) {
                                    try { innerA.click(); } catch(e) {}
                                }
                            }
                            return "Clicked question tab matching title '" + (labelEl.innerText || el.innerText || '').trim() + "'";
                        }
                    }
                }

                // 2. Match by taskKey pattern: e.g. "2. Two Sum", "Question 2", "Problem 2", "2"
                const keyPatterns = [
                    new RegExp('^(?:question|task|problem|q)?\\s*' + taskKey + '[\\.:\\)\\s]', 'i'),
                    new RegExp('^(?:question|task|problem|q)\\s*' + taskKey + '$', 'i'),
                    new RegExp('^' + taskKey + '$')
                ];
                for (const el of allLinks) {
                    if (el.querySelector('.mantine-NavLink-chevron, [data-chevron], [class*="chevron"], [class*="rightSection"] svg') ||
                        el.getAttribute('aria-expanded') === 'true' ||
                        el.getAttribute('data-opened') === 'true' ||
                        el.querySelector('.mantine-NavLink-root, button[class*="NavLink"]')) {
                        continue;
                    }
                    const labelEl = el.querySelector('.mantine-NavLink-label, [class*="label"]') || el;
                    const txt = (labelEl.innerText || el.innerText || '').trim();
                    if (txt && keyPatterns.some(p => p.test(txt)) && txt.length < 60) {
                        expandParents(el);
                        try {
                            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                            el.click();
                        } catch(e) {
                            el.click();
                        }
                        if (el.tagName.toLowerCase() !== 'a') {
                            const innerA = el.querySelector('a');
                            if (innerA) {
                                try { innerA.click(); } catch(e) {}
                            }
                        }
                        return "Clicked question tab matching '" + txt + "'";
                    }
                }

                // 3. Fallback: Next question button
                const numKey = parseInt(taskKey, 10);
                if (numKey > 1) {
                    for (const el of allLinks) {
                        const txt = (el.innerText || '').trim();
                        if (/^next(\s+question|\s+problem)?$/i.test(txt)) {
                            expandParents(el);
                            el.click();
                            return "Clicked Next Question button";
                        }
                    }
                }
                return null;
            }
            """
            nav_result = await page.evaluate(nav_script, {"taskKey": task_key, "taskTitle": task_title})

            if nav_result:
                self._orch._log(f"🧭 Auto-navigated browser to {task.platform_task.title or f'Task #{task.queue_index + 1}'}: {nav_result}", "info")
                await asyncio.sleep(3)  # Wait for SPA problem statement and editor to update
        except Exception as e:
            print(f"Auto-navigation helper note: {e}")

    defensively_recover_dom_tq = _defensively_recover_dom_tq
    run_recon = _run_recon
    _navigate_to_task_question = navigate_to_task_question
