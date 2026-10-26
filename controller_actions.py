"""
controller_actions.py — Browser-use controller action handlers for Cossito.

Contains all @controller.action handlers extracted from
AgentOrchestrator._build_controller. Each action is registered on a
Controller instance and receives the orchestrator via closure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
import time
from typing import Any, Optional

from browser_use import Controller
from browser_use.agent.views import ActionResult

from editor_adapters import safe_write
from llm_provider import get_llm
from models import (
    AgentPhase,
    CodingTask,
    TaskState,
    TaskStatus,
    WorkspaceSnapshot,
)
from platform_adapters import detect_platform, wait_for_terminal, result_fingerprint
from state_machine import can_transition, transition
from verification import verify_submission
import sys

def _get_sym(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("agent_core")
    return getattr(mod, name, fallback) if mod is not None else fallback


def consume_suggestions_block(orchestrator, context_name: str = "", inline: bool = False) -> str:
    """Safely consumes and formats any active human user suggestions from control_flags."""
    if not hasattr(orchestrator, "control_flags") or not isinstance(orchestrator.control_flags, dict):
        return ""
    suggs = orchestrator.control_flags.get("suggestions")
    if not suggs or not isinstance(suggs, list):
        return ""
    suggs_list = list(suggs)
    orchestrator.control_flags["suggestions"].clear()
    suggs_copy = [str(s).strip() for s in suggs_list if str(s).strip()]
    if not suggs_copy:
        return ""
    ctx_msg = f" directly into {context_name}" if context_name else ""
    orchestrator._log(f"💬 Active human suggestions injected{ctx_msg}: {suggs_copy}", "success")
    if inline:
        return f" IMPORTANT: New instructions/suggestions from user: {'; '.join(suggs_copy)}."
    return (
        "\n\n🚨 ACTIVE HUMAN USER DIRECTIVES (YOU MUST SOLVE AND COMPLY WITH THESE IMMEDIATELY):\n"
        + "\n".join(f"- {s}" for s in suggs_copy)
    )

def build_controller(orchestrator) -> Controller:
    """Create a Controller with custom agent actions."""
    controller = Controller()
    premature_completions = 0

    @controller.action('Finish reconnaissance, or finish execution only after verified completion. Report genuine inability with success=False and its reason.')
    async def done(text: str, success: bool = True) -> ActionResult:
        nonlocal premature_completions
        task = getattr(orchestrator, '_current_task', None)
        if task is None or not success or getattr(orchestrator, '_task_completed_successfully', False) or getattr(orchestrator, '_user_skip_requested', False):
            return ActionResult(is_done=True, success=success, extracted_content=text)
        cached = getattr(orchestrator, '_completion_write_baseline', None)
        if (getattr(orchestrator, '_code_written_in_current_task', False)
                and cached and cached[0][0] == getattr(task, 'task_id', None)):
            await verify_submission_status()
            if getattr(orchestrator, '_task_completed_successfully', False):
                return ActionResult(is_done=True, success=True, extracted_content=text)
        premature_completions += 1
        wrote_code = getattr(orchestrator, '_code_written_in_current_task', False)
        instruction = (
            'Use write_code_to_editor with the solution, then submit_code_solution to validate the current task.'
            if not wrote_code else
            'Use submit_code_solution or verify_submission_status to obtain current completion evidence. Do not rewrite working code just to finish.'
        )
        message = f'Completion rejected: this task has no verified result. {instruction}'
        orchestrator._log(message, 'warn')
        if premature_completions >= 3:
            reason = f'The model repeatedly requested completion without verification. Last explanation: {text}'
            return ActionResult(is_done=True, success=False, extracted_content=reason)
        # Feedback is not a tool error: the agent gets another step to act on it
        # without triggering the provider-failure threshold or restarting the run.
        return ActionResult(is_done=False, extracted_content=message, long_term_memory=message)

    async def completion_baseline(page, task, code):
        key = (getattr(task, 'task_id', None), hashlib.sha256(code.encode('utf-8')).hexdigest())
        cached = getattr(orchestrator, '_completion_write_baseline', None)
        if cached and cached[0] == key:
            return cached[1]
        adapter = await _get_sym('detect_platform', detect_platform)(page)
        record = await asyncio.wait_for(adapter.parse_result(page), timeout=10.0)
        orchestrator._completion_write_baseline = (key, record)
        return record

    @controller.action(
        "Extract the complete, raw, untruncated problem description, sample inputs/outputs, and constraints directly from the DOM. "
        "Call this action immediately as your very first step in reconnaissance to bypass vision viewport scrolling limits."
    )
    async def extract_problem_description(target_selector: Optional[str] = None) -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside extract_problem_description. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "problem description")

        orchestrator._log("🔍 Extracting full problem statement directly from DOM...")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
        except Exception as e:
            orchestrator._log(f"⚠️ Failed to get browser page: {e}", "warn")
            return f"Error: Active browser page is currently unavailable for extraction: {e}{suggestions_block}"

        if page is None:
            return f"Error: Active browser page is currently unavailable for extraction.{suggestions_block}"

        try:
            extracted_text, section_questions, embedded_images = await orchestrator._extract_problem_from_dom_direct(page, target_selector)
        except Exception as dom_err:
            orchestrator._log(f"⚠️ DOM extraction failed: {dom_err}", "warn")
            return f"Error: Failed to extract problem description from DOM: {dom_err}{suggestions_block}"
        orchestrator._detected_section_questions = section_questions

        question_nav_summary = ""
        if section_questions:
            question_nav_summary = "\n\n📌 DETECTED QUESTION / TASK NAVIGATION LIST (Total: " + str(len(section_questions)) + "):\n"
            for i, q in enumerate(section_questions):
                status = " [STATUS: ALREADY COMPLETED / PASSED ✅]" if (isinstance(q, dict) and q.get("completed")) else " [STATUS: PENDING / UNSOLVED ⏳]"
                q_name = q.get("text", "") if isinstance(q, dict) else str(q)
                question_nav_summary += str(i + 1) + ". " + q_name + status + "\n"

        extracted_text = (extracted_text or "") + question_nav_summary
        orchestrator._log(f"🔍 DOM extraction successful ({len(extracted_text)} characters, {len(embedded_images or [])} embedded images found).", "success")

        # ---- Automated Visual Analysis for Frontend/Design Tasks ----
        text_lower = extracted_text.lower()
        image_ref_indicators = [
            "refer to the below image", "refer below image", "refer to the below images",
            "as shown in the image", "as shown in below image", "as shown in below images",
            "design as in image", "look at the image", "refer mockup", "design mockup",
            "reference image", "refer image below", "refer to the image below", "refer to the images below",
            "as per the image", "as per the below image", "see the image", "see the below image",
            "given image", "given below image", "expected output image"
        ]
        
        has_image_ref = any(indicator in text_lower for indicator in image_ref_indicators)
        visual_details = ""

        if has_image_ref or len(embedded_images) > 0:
            orchestrator._log(
                f"📸 Detected {'image references in description' if has_image_ref else ''}"
                f"{'and ' if has_image_ref and embedded_images else ''}"
                f"{str(len(embedded_images)) + ' embedded <img> tags' if embedded_images else ''}. "
                f"Initiating comprehensive visual analysis...", "info"
            )

            try:
                # Select the right vision model: user's model if vision-capable, else Gemma 4 fallback
                if orchestrator.use_vision:
                    orchestrator._log("👁️ Using user's vision-capable model for image analysis.", "info")
                    vision_llm = get_llm(
                        model=orchestrator.model,
                        api_key=orchestrator.api_key,
                        provider=orchestrator.provider,
                        base_url=orchestrator.base_url,
                    )
                else:
                    orchestrator._log("🧠 Using Gemma 4 vision assistant for image analysis.", "info")
                    from llm_provider import get_vision_llm
                    vision_llm = get_vision_llm(
                        api_key=orchestrator.api_key,
                        provider=orchestrator.provider,
                    )

                from langchain_core.messages import HumanMessage
                all_visual_specs = []

                # Strategy 1: Screenshot individual <img> elements for pixel-perfect analysis
                if embedded_images:
                    for idx, img_info in enumerate(embedded_images[:5]):  # Limit to 5 images
                        css_path = img_info.get("cssPath", "")
                        img_src = img_info.get("src", "")
                        img_alt = img_info.get("alt", "")
                        
                        orchestrator._log(f"📸 Capturing embedded image {idx+1}/{min(len(embedded_images), 5)}: {img_alt or img_src[:60]}...", "info")
                        
                        try:
                            b64_img = None
                            # Strategy A: Use Playwright-native element screenshot (most reliable, bypasses CORS)
                            if css_path:
                                try:
                                    await page.evaluate(f"""(cssPath) => {{
                                        const el = document.querySelector(cssPath);
                                        if (el) el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                                    }}""", css_path)
                                    await asyncio.sleep(0.5)
                                    import base64
                                    raw_bytes = await browser.screenshot_element(css_path, format="png")
                                    if raw_bytes:
                                        b64_img = base64.b64encode(raw_bytes).decode("utf-8")
                                except Exception:
                                    pass  # Fall through to canvas/fetch strategies

                            # Strategy B: Canvas-based capture (works for same-origin images)
                            if not b64_img and css_path:
                                try:
                                    b64_img = await page.evaluate(f"""(cssPath) => (async () => {{
                                        const el = document.querySelector(cssPath);
                                        if (!el) return null;
                                        el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                                        await new Promise(r => setTimeout(r, 500));
                                        const canvas = document.createElement('canvas');
                                        const rect = el.getBoundingClientRect();
                                        canvas.width = rect.width;
                                        canvas.height = rect.height;
                                        const ctx = canvas.getContext('2d');
                                        ctx.drawImage(el, 0, 0, rect.width, rect.height);
                                        return canvas.toDataURL('image/png').split(',')[1];
                                    }})()""", css_path)
                                except Exception:
                                    pass

                            # Strategy C: Fetch the image URL directly
                            if not b64_img and img_src:
                                b64_img = await page.evaluate(f"""(url) => (async () => {{
                                    try {{
                                        const resp = await fetch(url);
                                        const blob = await resp.blob();
                                        return new Promise((resolve) => {{
                                            const reader = new FileReader();
                                            reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                            reader.readAsDataURL(blob);
                                        }});
                                    }} catch(e) {{ return null; }}
                                }})()""", img_src)

                            if b64_img:
                                prompt = f"""You are analyzing a reference/mockup design image for a frontend/webdev coding assignment.
Image context: {img_alt or 'Design mockup'}
Source: {img_src[:100]}

Provide an EXTREMELY detailed, structured, pixel-perfect specification of this design image including:
1. LAYOUT: Flexbox/grid structure, container hierarchy, alignment (row/column, justify-content, align-items)
2. DIMENSIONS: Exact widths, heights, padding, margins, gaps between elements
3. TYPOGRAPHY: Font family, font size, font weight, color, text-transform, letter-spacing, line-height for EACH text element
4. COLORS: Exact hex/rgb colors for backgrounds, borders, text, shadows (use descriptive names if exact values unclear)
5. BORDERS & SHADOWS: Border width, style, color, border-radius, box-shadow values
6. COMPONENTS: Each visible UI component (buttons, cards, lists, headers, inputs, images) with their exact CSS properties
7. SPACING: Exact gap/margin/padding between all elements
8. TEXT CONTENT: All visible text labels, headings, paragraphs exactly as shown

Reply ONLY with the structured specification. No preambles, no chit-chat.
"""
                                message = HumanMessage(content=[
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                                ])
                                response = await vision_llm.ainvoke([message])
                                desc = response.content if hasattr(response, "content") else str(response)
                                all_visual_specs.append(f"--- MOCKUP IMAGE {idx+1} ({img_alt or 'Design Reference'}) ---\n{desc.strip()}")
                                orchestrator._log(f"✅ Image {idx+1} analysis complete ({len(desc)} chars).", "success")
                        except Exception as img_err:
                            orchestrator._log(f"⚠️ Failed to analyze embedded image {idx+1}: {img_err}", "warn")

                # Strategy 2: Multi-viewport scroll capture if no embedded images were found/analyzed
                if not all_visual_specs:
                    orchestrator._log("📸 No embedded images captured. Taking multi-viewport scroll screenshots...", "info")
                    try:
                        # Take viewport screenshot at current position
                        b64_img = await asyncio.wait_for(page.screenshot(format="png"), timeout=15.0)
                        
                        prompt = """You are acting as the visual eyes for a text-only browser agent.
The agent is solving a frontend/webdev assignment and the description says "refer to the below images".
Examine the screenshot of the page carefully to locate the design mockup / reference image.
Provide a highly detailed, layout-by-layout, component-by-component description of the mockup (including exact HTML structure, flexbox layout, dimensions, alignments, colors, spacings, borders, font styles, text content, and CSS rules needed to duplicate it pixel-for-pixel).
Reply only with this objective visual layout specification. No preambles, no chit-chat.
"""
                        message = HumanMessage(content=[
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                        ])
                        response = await vision_llm.ainvoke([message])
                        desc = response.content if hasattr(response, "content") else str(response)
                        all_visual_specs.append(f"--- VIEWPORT SCREENSHOT ANALYSIS ---\n{desc.strip()}")
                        orchestrator._log("✅ Viewport screenshot analysis complete.", "success")

                        # Scroll down and take another screenshot to capture below-fold content
                        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
                        await asyncio.sleep(1)
                        b64_img2 = await asyncio.wait_for(page.screenshot(format="png"), timeout=15.0)
                        
                        message2 = HumanMessage(content=[
                            {"type": "text", "text": "Continue analyzing: This is a scrolled-down view of the same page. Describe any additional design mockup images, components, or layout specifications visible here that were NOT in the previous viewport."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img2}"}},
                        ])
                        response2 = await vision_llm.ainvoke([message2])
                        desc2 = response2.content if hasattr(response2, "content") else str(response2)
                        if desc2.strip() and len(desc2.strip()) > 50:
                            all_visual_specs.append(f"--- SCROLLED VIEWPORT ANALYSIS ---\n{desc2.strip()}")
                            orchestrator._log("✅ Scrolled viewport analysis complete.", "success")

                        # Scroll back to top
                        await page.evaluate("() => window.scrollTo(0, 0)")
                    except Exception as vp_err:
                        orchestrator._log(f"⚠️ Multi-viewport capture failed: {vp_err}", "warn")

                if all_visual_specs:
                    visual_details = "\n\n🎨 VISUAL DESIGN SPECIFICATION FROM REFERENCE IMAGES:\n" + "\n\n".join(all_visual_specs) + "\n--- END VISUAL DESIGN SPECIFICATION ---\n"
                    orchestrator._log(f"📷 Complete visual design spec generated ({len(visual_details)} chars total).", "success")

            except Exception as img_err:
                orchestrator._log(f"⚠️ Visual analysis failed: {img_err}", "warn")
                visual_details = f"\n\n⚠️ (Visual analysis failed: {img_err})\n"

        return f"SUCCESSFULLY EXTRACTED PROBLEM DESCRIPTION DIRECTLY FROM DOM:\n\n{extracted_text}{visual_details}{suggestions_block}"

    @controller.action(
        "Request a visual description of the web page or a specific element. "
        "Use this tool ONLY when vision is disabled to visually inspect code editors, output consoles, buttons, or page elements."
    )
    async def describe_page_visuals(query: str, target_selector: Optional[str] = None) -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside describe_page_visuals. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "visual description")

        try:
            orchestrator._log("📷 Capturing page screenshot for visual analysis...")
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)

            if page is None:
                orchestrator._log("⚠️ Could not retrieve or reopen active page for screenshot.", "warn")
                return f"Error: No active browser page available to capture screenshot.{suggestions_block}"

            # Scroll selector into view if provided to ensure it is in the screenshot
            if target_selector:
                try:
                    await page.evaluate(f"""() => {{
                        const el = document.querySelector("{target_selector}");
                        if (el) {{
                            el.scrollIntoView({{behavior: 'instant', block: 'center', inline: 'center'}});
                        }}
                    }}""")
                except Exception as e:
                    orchestrator._log(f"⚠️ Could not scroll selector '{target_selector}' into view: {e}", "warn")

            # Take screenshot as base64 string with overlay panel hidden
            async def _take_shot():
                try:
                    return await asyncio.wait_for(page.screenshot(format="png"), timeout=15.0)
                except asyncio.TimeoutError:
                    orchestrator._log("⚠️ Screenshot timeout: CDP session did not respond within 15 seconds. Retrying once...", "warn")
                    return await asyncio.wait_for(page.screenshot(format="png"), timeout=20.0)

            try:
                b64_img = await orchestrator._with_overlay_hidden(page, _take_shot)
            except Exception as e:
                orchestrator._log(f"⚠️ Failed to capture screenshot: {e}", "warn")
                return f"Error: Could not take screenshot: {e}{suggestions_block}"

            # Select the right vision model: user's model if vision-capable, else Gemma 4 fallback
            if orchestrator.use_vision:
                orchestrator._log("👁️ Using user's vision-capable model for visual description...")
                try:
                    vision_llm = get_llm(
                        model=orchestrator.model,
                        api_key=orchestrator.api_key,
                        provider=orchestrator.provider,
                        base_url=orchestrator.base_url,
                    )
                except Exception as e:
                    orchestrator._log(f"⚠️ Failed to load user's model for vision, falling back to Gemma 4: {e}", "warn")
                    from llm_provider import get_vision_llm
                    vision_llm = get_vision_llm(api_key=orchestrator.api_key, provider=orchestrator.provider)
            else:
                orchestrator._log("🧠 Querying Gemma 4 / Gemini vision assistant...")
                try:
                    from llm_provider import get_vision_llm
                    vision_llm = get_vision_llm(
                        api_key=orchestrator.api_key,
                        provider=orchestrator.provider,
                    )
                except Exception as e:
                    orchestrator._log(f"⚠️ Failed to load vision assistant model: {e}", "warn")
                    return f"Error: Could not initialize vision assistant model. Ensure you have entered a valid API Key. Detail: {e}{suggestions_block}"

            # Formulate the strict communication prompt
            prompt = f"""You are acting as the visual eyes for a text-only browser agent.
The agent wants to know: "{query}"

Analyze the provided screenshot of the web page and reply with a highly precise, objective, and structured description.
- Describe only what is visually present in the screenshot related to the query.
- Include exact text from code editors, console outputs, button labels, warnings, or error panels.
- Do NOT add any conversational chit-chat, side explanations, or standard LLM preambles.
- Keep the response extremely direct and strictly factual.
"""

            # Call the vision model with the image and text prompt
            try:
                from langchain_core.messages import HumanMessage
                message = HumanMessage(
                    content=[
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_img}"},
                        },
                    ]
                )
                response = await vision_llm.ainvoke([message])
                description = response.content if hasattr(response, "content") else str(response)
                
                # Clean up response
                description = description.strip()
                orchestrator._log("📷 Visual description successfully received from vision assistant.", "success")
                
                return f"VISUAL DESCRIPTION FROM GEMMA 4 VISION ASSISTANT:\n\n{description}{suggestions_block}"
            except Exception as e:
                orchestrator._log(f"❌ Vision assistant invocation failed: {e}", "error")
                return f"Error: Vision assistant failed to analyze the image: {e}{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Error executing describe_page_visuals: {e}", "warn")
            return f"Error: Failed to describe page visuals: {e}{suggestions_block}"

    @controller.action(
        "Extract all design mockup or reference images from the problem description page, screenshot them, and save them in the workspace. "
        "Use once when the task supplies reference images, then inspect only the returned files with view_file. "
        "An empty result is not a blocker: continue implementing the written requirements."
    )
    async def extract_and_view_reference_images() -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside extract_and_view_reference_images. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "extract_and_view_reference_images")

        try:
            orchestrator._log("📸 Extracting reference images from problem statement...")
            saved = await orchestrator._extract_ref_images_to_disk()
            if not saved:
                return (
                    "Reference check complete: no usable reference images were found or could be saved. "
                    "Do not repeat extraction or call view_file without a returned path. "
                    "Continue now: read_code_from_editor, implement the written requirements, "
                    f"and verify the rendered preview after coding.{suggestions_block}"
                )

            report_lines = [f"Successfully extracted and saved {len(saved)} reference images:"]
            for img in saved:
                report_lines.append(
                    f"- Image {img['index']} (Alt: '{img['alt']}', Src: '{img['src'][:100]}'):\n"
                    f"  Absolute Path: {img['file_path']}\n"
                    f"  🚨 ACTION REQUIRED: Call the `view_file` tool on absolute path '{img['file_path']}' to inspect this mockup!"
                )

            return "\n".join(report_lines) + suggestions_block
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Failed to extract reference images: {e}", "warn")
            return (
                f"Reference inspection unavailable: {e}. Continue from the written requirements; "
                f"do not wait for images or repeat extraction.{suggestions_block}"
            )

    @controller.action(
        "Skip the current task and proceed immediately to the next task in the TaskQueue. "
        "Call this when the page is an introduction/reading/video page with no code editor, or when instructed by the human user to skip."
    )
    async def skip_current_task(reason: str = "Intro page or user requested skip") -> str:
        suggestions_block = consume_suggestions_block(orchestrator, "skip_current_task")
        orchestrator._log(f"⏭️ Skipping Task #{orchestrator.state.current_task_index + 1}: {reason}", "warn")
        orchestrator._code_written_in_current_task = True
        orchestrator._last_report = {
            "task_id": orchestrator.state.current_task_index + 1,
            "success": True,
            "output": f"Task skipped: {reason}",
        }
        return f"Task #{orchestrator.state.current_task_index + 1} skipped successfully. Call `done` now to advance to the next task.{suggestions_block}"

    @controller.action(
        "Pause execution and alert the human user to log in or solve a CAPTCHA."
    )
    async def pause_for_human(reason: str) -> str:
        orchestrator._log(f"⏸️ HITL pause: {reason}", "warn")
        orchestrator._set_phase(AgentPhase.PAUSED_HITL)
        orchestrator.state.hitl_reason = reason
        orchestrator._notify()
        await orchestrator.hitl.trigger_pause(reason)
        orchestrator._submission_check_window = None
        # When we get here, the human has resumed.
        orchestrator._set_phase(AgentPhase.EXECUTING)
        orchestrator.state.hitl_reason = None
        orchestrator._notify()
        orchestrator._log("▶️ Resumed by user.", "success")
        suggestions_block = consume_suggestions_block(orchestrator, "pause_for_human")
        return f"User has logged in / solved CAPTCHA. Continuing.{suggestions_block}"

    @controller.action(
        "Report the result of a coding task after execution."
    )
    async def report_task_result(
        task_id: int, success: bool, output: str
    ) -> str:
        suggestions_block = consume_suggestions_block(orchestrator, "report_task_result")
        if success and not getattr(orchestrator, "_code_written_in_current_task", False):
            return (
                "❌ CANNOT REPORT SUCCESS: You have NOT written any code solution to the editor yet!\n"
                f"You MUST first write the solution code using `write_code_to_editor` or `submit_code_solution`, and verify test cases before reporting success!{suggestions_block}"
            )

        if success and not getattr(orchestrator, '_task_completed_successfully', False):
            return ('Cannot report success without a verified platform submission. '
                    'Submit the solution and call verify_submission_status; remain on this task.' + suggestions_block)

        orchestrator._last_report = {
            "task_id": task_id,
            "success": success,
            "output": output,
        }
        status = "SUCCESS" if success else "FAILED"
        orchestrator._log(
            f"   📝 Task #{task_id} reported: {status} — {output[:200]}"
        )
        if success:
            orchestrator._task_completed_successfully = True
            if hasattr(orchestrator, "state") and orchestrator.state is not None:
                orchestrator.state.model_thinking = None
            orchestrator._notify()
            if getattr(orchestrator, "_current_agent", None) and hasattr(orchestrator._current_agent, "stop"):
                try:
                    orchestrator._current_agent.stop()
                except Exception:
                    pass
            if orchestrator._current_agent_task and not orchestrator._current_agent_task.done():
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_soon(orchestrator._current_agent_task.cancel)
                except Exception:
                    try:
                        loop = asyncio.get_event_loop()
                        loop.call_soon(orchestrator._current_agent_task.cancel)
                    except Exception:
                        pass
        return f"Result recorded for Task #{task_id}.{suggestions_block}"

    @controller.action(
        "Verify the platform submission status by parsing test results from the DOM. Call this action AFTER clicking 'Submit'."
    )
    async def verify_submission_status(info: str = "check") -> str:
        """Scans DOM for test results, passed/failed test cases, and returns a verified verdict."""
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside verify_submission_status. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "verify_submission_status")
        try:
            current_task = orchestrator._current_task
            if current_task is None and orchestrator.state.task_queue and orchestrator.state.task_queue.tasks:
                current_task = orchestrator.state.task_queue.tasks[orchestrator.state.current_task_index]

            if current_task is None:
                return 'No active task is available to verify.' + suggestions_block
            if getattr(orchestrator, '_submission_started_task_id', None) != current_task.task_id:
                cached = getattr(orchestrator, '_completion_write_baseline', None)
                if (not getattr(orchestrator, '_code_written_in_current_task', False)
                        or not cached or cached[0][0] != current_task.task_id):
                    return 'No tracked code write or validation attempt exists for this task.' + suggestions_block
                # Automatic validation and native Run Tests clicks are legitimate
                # workflows. Require fresh evidence relative to the tracked write.
                orchestrator._submission_baseline = cached[1]
                orchestrator._submission_started_task_id = current_task.task_id
                orchestrator._submission_observed_pending = False
                orchestrator._submission_check_window = None

            task_key = getattr(current_task, 'task_id', None)
            window = getattr(orchestrator, '_submission_check_window', None)
            if window is None or window[0] != task_key:
                window = (task_key, time.monotonic())
                orchestrator._submission_check_window = window
            remaining = 60.0 - (time.monotonic() - window[1])

            async def read_submission():
                browser = await orchestrator.browser_mgr.get_browser()
                page = await orchestrator._get_current_page_safe(browser)
                if page is None:
                    raise RuntimeError('Active page unavailable')
                adapter = await _get_sym("detect_platform", detect_platform)(page)
                return await adapter.parse_result(page)

            # The polling deadline must not hide a verdict that arrived later.
            # Each explicit check gets one bounded read, never a new polling loop.
            record = await asyncio.wait_for(read_submission(), timeout=10.0)

            baseline = getattr(orchestrator, '_submission_baseline', None)
            if not record.terminal:
                orchestrator._submission_observed_pending = True
            if (record.terminal and baseline is not None
                    and not getattr(orchestrator, '_submission_observed_pending', False)
                    and result_fingerprint(record) == result_fingerprint(baseline)):
                return ('The visible verdict has not changed from before this attempt. '
                        'It is not proof of a new submission. Stay on this task and inspect its submission status.' + suggestions_block)

            if current_task:
                record.task_id = current_task.task_id
                if current_task.workspace and current_task.workspace.content_hash:
                    record.code_hash = current_task.workspace.content_hash
                elif current_task.solution_code:
                    record.code_hash = hashlib.sha256(current_task.solution_code.encode("utf-8")).hexdigest()[:16]

            if not record.terminal:
                if remaining <= 0:
                    return ('Submission verification deadline reached; the outcome is unknown. '
                            'Do not reload, rewrite, resubmit, or keep checking. Call pause_for_human '
                            'to inspect the existing submission and preserve the page.' + suggestions_block)
                return f"No final submission verdict is visible yet. Preserve the page and code; do not reload or resubmit. Wait briefly before checking again.{suggestions_block}"

            orchestrator._submission_check_window = None

            success, v_result = _get_sym("verify_submission", verify_submission)(record, current_task or CodingTask())

            if success:
                if current_task:
                    current_task.submissions.append(record)
                    current_task.status = TaskStatus.SUCCESS
                    if can_transition(current_task, TaskState.PASSED):
                        transition(current_task, TaskState.PASSED)
                    else:
                        current_task.state = TaskState.PASSED
                orchestrator._last_report = {
                    "task_id": (orchestrator.state.current_task_index + 1) if (hasattr(orchestrator, "state") and orchestrator.state is not None) else 1,
                    "success": True,
                    "output": v_result.details,
                }
                orchestrator._task_completed_successfully = True
                if hasattr(orchestrator, "state") and orchestrator.state is not None:
                    orchestrator.state.model_thinking = None
                orchestrator._notify()
                if getattr(orchestrator, "_current_agent", None) and hasattr(orchestrator._current_agent, "stop"):
                    try:
                        orchestrator._current_agent.stop()
                    except Exception:
                        pass
                if orchestrator._current_agent_task and not orchestrator._current_agent_task.done():
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_soon(orchestrator._current_agent_task.cancel)
                    except Exception:
                        try:
                            loop = asyncio.get_event_loop()
                            loop.call_soon(orchestrator._current_agent_task.cancel)
                        except Exception:
                            pass
                orchestrator._log(f"✅ Verified submission: {v_result.details}", "success")
                return (
                    f"✅ SUBMISSION VERIFIED SUCCESSFUL: {v_result.details}.\n"
                    f"Your task is 100% complete. Call report_task_result(success=True) and done.{suggestions_block}"
                )
            else:
                orchestrator._last_report = {
                    "task_id": orchestrator.state.current_task_index + 1,
                    "success": False,
                    "output": v_result.details,
                }
                orchestrator._log(f"⚠️ Submission not fully passed: {v_result.details}", "warn")
                return (
                    f"❌ SUBMISSION FAILED / PARTIAL: {v_result.details}.\n"
                    f"Analyze errors, fix code with write_code_to_editor, and submit again.{suggestions_block}"
                )

        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return ('Submission status read timed out; the outcome is unknown. Do not reload '
                    'or resubmit. Call pause_for_human if the page remains unresponsive.' + suggestions_block)
        except Exception as e:
            return f"Error verifying submission status: {e}{suggestions_block}"

    @controller.action(
        "Check if the human user has injected any custom suggestions, hints, class specifications, or correction instructions."
    )
    async def get_human_suggestions(info: str = "check") -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "get_human_suggestions", inline=True)
        if suggestions_block:
            return f"Human User Direct Instructions retrieved:{suggestions_block}"
        return "No custom suggestions from the human user at this time. Proceed standardly."

    @controller.action(
        "Write the complete self-contained code solution into the active code editor on the page. "
        "Automatically takes a snapshot, verifies editor kind, and confirms write via readback."
    )
    async def write_code_to_editor(code: str) -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside write_code_to_editor. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "write_code_to_editor")
        orchestrator._submission_started_task_id = None
        orchestrator._task_completed_successfully = False
        orchestrator._last_report = None

        try:
            orchestrator._log("⚡ Writing complete code solution into editor...")
            orchestrator._code_written_in_current_task = True
            
            # Display the code in the Web UI immediately as the current solution code
            orchestrator.state.current_solution_code = code
            orchestrator.state.failed_injection_code = None
            orchestrator._notify()

            current_task = orchestrator._current_task
            if current_task is None and orchestrator.state.task_queue and orchestrator.state.task_queue.tasks:
                current_task = orchestrator.state.task_queue.tasks[orchestrator.state.current_task_index]

            if current_task:
                current_task.solution_code = code
                if can_transition(current_task, TaskState.EDITING):
                    transition(current_task, TaskState.EDITING)

            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active page unavailable.{suggestions_block}"

            await completion_baseline(page, current_task, code)
            for attempt in range(1, 4):
                orchestrator._log(f"⚡ Editor write attempt {attempt}/3...")
                try:
                    success, snap_before, readback = await _get_sym("safe_write", safe_write)(page, code)
                    if not success:
                        direct_res = await orchestrator._inject_code_direct(browser, code)
                        if direct_res:
                            success = True
                            snap_before = WorkspaceSnapshot(editor_kind="direct_injected", content=code)
                            orchestrator._log(f"⚡ Direct editor injection succeeded: {direct_res}", "success")
                    if success:
                        new_snap = WorkspaceSnapshot(
                            editor_kind=snap_before.editor_kind or "editor",
                            content=code,
                            language=snap_before.language,
                            model_id=snap_before.model_id,
                        )
                        new_snap.compute_hash()
                        if current_task:
                            current_task.workspace = new_snap
                            current_task.solution_code = code
                        editor_name = new_snap.editor_kind or "editor"
                        orchestrator._log(f"⚡ Editor update successful & verified ({editor_name}).", "success")
                        orchestrator.state.current_solution_code = None
                        orchestrator.state.failed_injection_code = None
                        orchestrator._notify()
                        return f"Success: Code written and verified in {editor_name}. Call submit_code_solution to discover and verify this page's completion workflow.{suggestions_block}"
                except Exception as e:
                    orchestrator._log(f"⚠️ Attempt {attempt} failed with exception: {e}", "warn")

                if attempt < 3:
                    await asyncio.sleep(1.0)

            orchestrator._log("❌ All 3 code injection attempts failed. Displaying solution code in Web UI.", "error")
            orchestrator.state.current_solution_code = None
            orchestrator.state.failed_injection_code = code
            orchestrator._notify()
            return f"Failed: No supported editor container found after 3 attempts.{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"❌ Error in write_code_to_editor: {e}", "error")
            return f"Error writing code to editor: {e}{suggestions_block}"

    @controller.action(
        "Write the solution and discover the page's completion workflow: Submit, Check, Run Tests, or automatic validation. "
        "Verify completion without clicking Next prematurely. This also supports pages without a Submit button. "
        "For a custom validation control, pass completion_control with its exact observed visible label; never invent a label or select navigation."
    )
    async def submit_code_solution(code: str, completion_control: Optional[str] = None) -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "submit_code_solution")

        try:
            orchestrator._log("Writing the solution and discovering the page's completion workflow...")
            orchestrator._code_written_in_current_task = True
            orchestrator.state.current_solution_code = code
            orchestrator._notify()

            current_task = orchestrator._current_task
            if current_task is None and orchestrator.state.task_queue and orchestrator.state.task_queue.tasks:
                current_task = orchestrator.state.task_queue.tasks[orchestrator.state.current_task_index]

            if current_task:
                current_task.solution_code = code

            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active page unavailable.{suggestions_block}"

            # 1. Safe write code
            baseline = await completion_baseline(page, current_task, code)
            write_success, snap_before, readback = await _get_sym("safe_write", safe_write)(page, code)
            if not write_success:
                direct_res = await orchestrator._inject_code_direct(browser, code)
                if direct_res:
                    write_success = True
                    snap_before = WorkspaceSnapshot(editor_kind="direct_injected", content=code)
                    orchestrator._log(f"⚡ Direct editor injection succeeded: {direct_res}", "success")
            if not write_success:
                orchestrator._code_written_in_current_task = False
                return f"Code could not be written to the editor. Submission was not attempted; inspect the editor before retrying.{suggestions_block}"
            
            new_snap = WorkspaceSnapshot(
                editor_kind=snap_before.editor_kind or "editor",
                content=code,
                language=snap_before.language,
                model_id=snap_before.model_id,
            )
            new_snap.compute_hash()
            if current_task:
                current_task.workspace = new_snap
                current_task.solution_code = code
                if can_transition(current_task, TaskState.SUBMITTING):
                    transition(current_task, TaskState.SUBMITTING)

            # 2. Submit via platform adapter
            adapter = await _get_sym("detect_platform", detect_platform)(page)
            orchestrator._submission_baseline = baseline
            orchestrator._submission_observed_pending = False
            existing = await asyncio.wait_for(adapter.parse_result(page), timeout=10.0)
            existing_success, _ = _get_sym("verify_submission", verify_submission)(existing, current_task or CodingTask())
            already_complete = existing_success and result_fingerprint(existing) != result_fingerprint(baseline)
            completion = ({'mode': 'automatic', 'started': False} if already_complete else
                          await asyncio.wait_for(adapter.start_completion(page, completion_control), timeout=10.0))
            started = completion.get('started', False)
            mode = completion.get('mode', 'unknown')
            if started or mode in ('automatic', 'navigation'):
                orchestrator._submission_started_task_id = current_task.task_id if current_task else None
                orchestrator._submission_check_window = (current_task.task_id if current_task else None, time.monotonic())
                orchestrator._log(f"Completion workflow: {mode}" + (f" — {completion.get('label', '')}" if started else ' — inspecting page evidence'), "info")
            else:
                return f"Completion was not started: the validation control is unavailable or disabled. Visible controls: {completion.get('controls', [])}. Inspect why it is unavailable; do not wait for a nonexistent submission.{suggestions_block}"

            if current_task and can_transition(current_task, TaskState.VERIFYING):
                transition(current_task, TaskState.VERIFYING)

            # 3. Wait for terminal result & verify
            def remember_pending():
                orchestrator._submission_observed_pending = True

            record = existing if already_complete else await _get_sym("wait_for_terminal", wait_for_terminal)(page, adapter, timeout=30.0 if started else 3.0, poll_interval=0.5, baseline=baseline, on_pending=remember_pending)
            orchestrator._log(f"Completion evidence: outcome={record.outcome}, terminal={record.terminal}, passed={record.passed_count}/{record.total_count}; {record.raw_output[:300]}", 'info')
            if current_task:
                record.task_id = current_task.task_id
                record.code_hash = new_snap.content_hash
                current_task.submissions.append(record)

            if not record.terminal:
                if not started:
                    orchestrator._submission_started_task_id = None
                    return (
                        f"This page uses {mode} completion and has no discovered Submit/Check/Run control. "
                        f"Observed controls: {completion.get('controls', [])}. No fresh completion evidence is visible. "
                        "Do not wait for Submit or click Next as proof of success. If an observed control validates this task, "
                        "call this action with completion_control set to its exact label. Otherwise inspect the rendered preview and "
                        "the page's completion requirements; request human confirmation if it provides no machine-readable verdict."
                        + suggestions_block
                    )
                return (
                    "Submission has no confirmed final result yet. Do not submit again or rewrite code "
                    "because of this wait timeout. Call verify_submission_status to inspect the existing "
                    f"submission; if it remains pending, report that status.{suggestions_block}"
                )

            success, v_result = _get_sym("verify_submission", verify_submission)(record, current_task or CodingTask())

            if success:
                if current_task:
                    current_task.status = TaskStatus.SUCCESS
                    if can_transition(current_task, TaskState.PASSED):
                        transition(current_task, TaskState.PASSED)
                    else:
                        current_task.state = TaskState.PASSED
                orchestrator._last_report = {
                    "task_id": (orchestrator.state.current_task_index + 1) if (hasattr(orchestrator, "state") and orchestrator.state is not None) else 1,
                    "success": True,
                    "output": v_result.details,
                }
                orchestrator._task_completed_successfully = True
                if hasattr(orchestrator, "state") and orchestrator.state is not None:
                    orchestrator.state.model_thinking = None
                orchestrator._notify()
                if getattr(orchestrator, "_current_agent", None) and hasattr(orchestrator._current_agent, "stop"):
                    try:
                        orchestrator._current_agent.stop()
                    except Exception:
                        pass
                if orchestrator._current_agent_task and not orchestrator._current_agent_task.done():
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_soon(orchestrator._current_agent_task.cancel)
                    except Exception:
                        try:
                            loop = asyncio.get_event_loop()
                            loop.call_soon(orchestrator._current_agent_task.cancel)
                        except Exception:
                            pass
                orchestrator._log(f"✅ Fast-Path Verified: {v_result.details}", "success")
                return f"✅ Fast-Path SUBMISSION VERIFIED SUCCESSFUL: {v_result.details}. Call report_task_result(success=True) and done.{suggestions_block}"
            else:
                orchestrator._last_report = {
                    "task_id": orchestrator.state.current_task_index + 1,
                    "success": False,
                    "output": v_result.details,
                }
                orchestrator._log(f"⚠️ Fast-Path Result: {v_result.details}", "warn")
                return f"❌ Fast-Path SUBMISSION FAILED: {v_result.details}. Analyze errors, fix code, and submit again.{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Fast-Path execution error: {e}", "warn")
            return f"Error executing submit_code_solution: {e}{suggestions_block}"

    @controller.action(
        "Execute a specialized learned skill from the local skill registry. "
        "Use this action to execute pre-compiled, parameterized skills like 'handle_blocking_modals', 'monaco_diff_injector', 'shadow_dom_navigator', or any distilled custom skill."
    )
    async def execute_learned_skill(skill_name: str, parameters: Optional[dict[str, Any]] = None) -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, f"execute_learned_skill ({skill_name})")

        params = parameters or {}
        orchestrator._log(f"⚡ Executing learned skill '{skill_name}' with parameters: {params}")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active browser page is not available to run skill '{skill_name}'.{suggestions_block}"

            result = await orchestrator.skill_registry.execute_skill(skill_name, page, **params)
            orchestrator._log(f"✅ Skill '{skill_name}' completed successfully: {result}", "success")
            return f"Skill '{skill_name}' execution result: {result}{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Error executing skill '{skill_name}': {e}", "warn")
            return f"Skill '{skill_name}' execution failed: {e}{suggestions_block}"

    @controller.action(
        "Read the current code content or boilerplate template from the active code editor on the page. "
        "Call this action BEFORE writing code to check if there is existing boilerplate that you must preserve."
    )
    async def read_code_from_editor() -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside read_code_from_editor. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "read_code_from_editor", inline=True)

        orchestrator._log("🔍 Reading current code content from editor...")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            
            if page is None:
                return f"Error: Active page unavailable to read code.{suggestions_block}"
            
            # Execute JS to read code from Monaco, CodeMirror, Ace, standard textareas, etc.
            code = await page.evaluate("""() => {
                function getEditorCodeRecursively(doc) {
                    if (!doc) return null;
                    
                    // 1. Monaco Editor (VS Code based)
                    try {
                        const win = doc.defaultView || window;
                        if (win.monaco && win.monaco.editor) {
                            const models = win.monaco.editor.getModels();
                            if (models && models.length > 0) {
                                return models[0].getValue();
                            }
                        }
                    } catch (e) {}
                    
                    // 2. CodeMirror (v5)
                    try {
                        const cmElems = doc.querySelectorAll('.CodeMirror');
                        for (const elem of cmElems) {
                            if (elem.CodeMirror) {
                                return elem.CodeMirror.getValue();
                            }
                        }
                    } catch (e) {}
                    
                    // 3. CodeMirror 6 / Modern Contenteditable editors
                    try {
                        const cm6Elems = doc.querySelectorAll('.cm-content');
                        for (const elem of cm6Elems) {
                            if (elem.contentEditable === "true") {
                                return elem.textContent || "";
                            }
                        }
                    } catch (e) {}
                    
                    // 4. Ace Editor
                    try {
                        const aceElems = doc.querySelectorAll('.ace_editor');
                        for (const elem of aceElems) {
                            const win = doc.defaultView || window;
                            if (win.ace && win.ace.edit) {
                                const editor = win.ace.edit(elem);
                                if (editor) {
                                    return editor.getValue();
                                }
                            }
                        }
                    } catch (e) {}
                    
                    // 5. General contenteditable elements
                    try {
                        const contenteditables = doc.querySelectorAll('[contenteditable="true"]');
                        for (const elem of contenteditables) {
                            return elem.textContent || "";
                        }
                    } catch (e) {}
                    
                    // 6. Standard Textareas
                    try {
                        const textareas = doc.querySelectorAll('textarea');
                        for (const elem of textareas) {
                            return elem.value;
                        }
                    } catch (e) {}

                    // 7. Same-origin iframes recursively
                    try {
                        const iframes = doc.querySelectorAll('iframe');
                        for (const iframe of iframes) {
                            try {
                                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                                if (iframeDoc) {
                                    const result = getEditorCodeRecursively(iframeDoc);
                                    if (result !== null) return result;
                                }
                            } catch (e) {}
                        }
                    } catch (e) {}
                    
                    return null;
                }
                
                return getEditorCodeRecursively(document);
            }""")
            
            if code is not None:
                orchestrator._log(f"✅ Read code from editor successful (length: {len(code)}).", "success")
                return (
                    f"Current Editor Content (length: {len(code)}):\n```\n{code}\n```\n{suggestions_block}\n\n"
                    f"🚨 MANDATORY NEXT STEP: Now implement your complete solution and call `write_code_to_editor(code=...)` to inject it into the editor!\n"
                    f"Do NOT call `done` or `report_task_result` without calling `write_code_to_editor` first!"
                )
            else:
                return (
                    f"No supported code editor or textarea found on the page.{suggestions_block}\n\n"
                    f"🚨 NO CODE EDITOR DETECTED: If this is an Introduction, Video, or Reading page, call `skip_current_task(reason='No code editor on page')` right now to skip to the next coding problem!"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Failed to read code from editor: {e}", "warn")
            return f"Error: Failed to read from editor: {e}{suggestions_block}"

    @controller.action(
        "Reset the code editor on the page to its default/original template state. "
        "Call this action immediately if you are instructed by the user to 'reset the code' or start over."
    )
    async def reset_editor() -> str:
        # Check for stop request
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            orchestrator._log("🛑 Stop signal detected inside reset_editor. Terminating immediately.", "warn")
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "reset_editor")

        orchestrator._log("🔄 Resetting code editor to default state...")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            
            if page is None:
                return f"Error: Active page unavailable to reset editor.{suggestions_block}"
            
            # Execute JS to find and click Reset button and confirm any dialog
            result = await page.evaluate("""() => (async () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                // Look for Reset button
                let resetBtn = buttons.find(b => b.innerText.toLowerCase().includes('reset'));
                if (!resetBtn) {
                    resetBtn = document.querySelector('[class*="reset"], [id*="reset"]');
                }
                if (resetBtn) {
                    resetBtn.click();
                    // Wait for modal to render
                    await new Promise(r => setTimeout(r, 600));
                    // Look for confirmation modal button
                    const modalButtons = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const confirmBtn = modalButtons.find(b => {
                        const txt = b.innerText.toLowerCase();
                        return (txt.includes('confirm') || txt.includes('yes') || txt.includes('reset') || txt.includes('agree')) && b !== resetBtn;
                    });
                    if (confirmBtn) {
                        confirmBtn.click();
                        return "Successfully clicked Reset button and confirmed the reset modal.";
                    }
                    return "Successfully clicked Reset button (no confirmation modal detected).";
                }
                return null;
            })()""")

            if result:
                orchestrator._log(f"🔄 Editor reset result: {result}", "success")
                return f"Success: {result}{suggestions_block}"
            else:
                orchestrator._log("⚠️ Could not find a Reset button on the page. Trying manual select-all and delete.", "warn")
                # Fallback: Clear editor using safe_write / DOM evaluate
                try:
                    cleared, _, _ = await safe_write(page, "")
                    if cleared:
                        return f"Cleared the editor manually using safe_write (no Reset button found on page).{suggestions_block}"
                    # Secondary evaluate clear
                    await page.evaluate("""() => {
                        for (const sel of ['.monaco-editor', '.CodeMirror', '.cm-content', '.ace_editor', 'textarea', '[contenteditable="true"]']) {
                            const el = document.querySelector(sel);
                            if (el) {
                                if ('value' in el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }
                                else { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }
                                return true;
                            }
                        }
                        return false;
                    }""")
                    return f"Cleared the editor manually using DOM evaluate (no Reset button found on page).{suggestions_block}"
                except Exception as e:
                    return f"Failed to reset editor: {e}{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            orchestrator._log(f"⚠️ Error resetting editor: {e}", "warn")
            return f"Failed to reset editor: {e}{suggestions_block}"

    @controller.action(
        "Inspect the semantic Accessibility Tree (AXTree) of the page to find interactive elements, buttons, inputs, tabs, and their ARIA labels/roles. "
        "Use this when visual selectors fail or elements are obscured."
    )
    async def inspect_accessibility_tree(max_depth: int = 5) -> str:
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "inspect_accessibility_tree")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active browser page is currently unavailable.{suggestions_block}"

            perception = getattr(orchestrator, "perception_engine", None)
            if perception is None:
                from devtools_perception import DevToolsPerceptionEngine
                perception = DevToolsPerceptionEngine()

            snapshot = await perception.get_accessibility_tree(browser, page, max_depth=max_depth)
            elements_text = "\n".join(snapshot.interactive_elements[:30]) if snapshot.interactive_elements else "No interactive accessibility nodes found."
            orchestrator._log(f"🔍 Accessibility tree inspected: {len(snapshot.interactive_elements)} interactive nodes discovered.", "info")
            return f"ACCESSIBILITY TREE INTERACTIVE NODES:\n{elements_text}{suggestions_block}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"Error inspecting accessibility tree: {e}{suggestions_block}"

    @controller.action(
        "Inspect recent runtime console logs, uncaught JavaScript exceptions, and compile errors from the page. "
        "Use this when debugging why code execution failed or why a button is unresponsive."
    )
    async def inspect_console_logs() -> str:
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "inspect_console_logs")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active browser page is currently unavailable.{suggestions_block}"

            perception = getattr(orchestrator, "perception_engine", None)
            if perception is None:
                from devtools_perception import DevToolsPerceptionEngine
                perception = DevToolsPerceptionEngine()

            errors = await perception.get_console_errors(page)
            if not errors:
                return f"No console errors or unhandled exceptions recorded on page.{suggestions_block}"

            err_lines = [f"[{e.level.upper()}] {e.text} ({e.source})" for e in errors[-10:]]
            return f"RECENT CONSOLE ERRORS / EXCEPTIONS:\n" + "\n".join(err_lines) + suggestions_block
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"Error inspecting console logs: {e}{suggestions_block}"

    @controller.action(
        "Inspect recent network failures, HTTP 4xx/5xx status codes, and blocked requests on the page. "
        "Use this when judging submission requests fail or server responses hang."
    )
    async def inspect_network_activity() -> str:
        if hasattr(orchestrator, "control_flags") and orchestrator.control_flags and orchestrator.control_flags.get("stop_requested"):
            raise asyncio.CancelledError("Stop requested by user.")

        suggestions_block = consume_suggestions_block(orchestrator, "inspect_network_activity")
        try:
            browser = await orchestrator.browser_mgr.get_browser()
            page = await orchestrator._get_current_page_safe(browser)
            if page is None:
                return f"Error: Active browser page is currently unavailable.{suggestions_block}"

            perception = getattr(orchestrator, "perception_engine", None)
            if perception is None:
                from devtools_perception import DevToolsPerceptionEngine
                perception = DevToolsPerceptionEngine()

            failures = await perception.get_network_failures(page)
            if not failures:
                return f"No network failures or HTTP errors detected.{suggestions_block}"

            fail_lines = [f"[{f.method}] {f.url} -> Status {f.status_code or 'FAILED'} ({f.error_text or 'error'})" for f in failures[-10:]]
            return f"RECENT NETWORK FAILURES / HTTP ERRORS:\n" + "\n".join(fail_lines) + suggestions_block
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"Error inspecting network activity: {e}{suggestions_block}"

    return controller
