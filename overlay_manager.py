"""
overlay_manager.py — Browser overlay panel lifecycle for OmniTasker.

Manages injection, state synchronization, and command polling for
the glassmorphism overlay panel injected into target web pages.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional


class OverlayManager:
    """Manages the in-browser overlay panel lifecycle.
    
    Holds references to the orchestrator for state access,
    browser management, and logging.
    """

    def __init__(self, orchestrator):
        """Accept the orchestrator instance for state/browser/logging access."""
        self._orch = orchestrator
        self._injecting_pages: set = set()

    async def _background_poll_loop(self) -> None:
        """Continuously syncs state and polls user commands from all browser pages."""
        while not self._orch._stop_requested:
            if self._orch.control_flags.get("stop_requested"):
                self._orch._stop_requested = True
                self._orch._log("🛑 Stop signal detected. Cancelling current agent task...", "warn")
                if hasattr(self._orch, "_current_agent_task") and self._orch._current_agent_task and not self._orch._current_agent_task.done():
                    self._orch._current_agent_task.cancel()
                break

            try:
                if self._orch.browser_mgr.is_open:
                    browser = await self._orch.browser_mgr.get_browser()
                    if getattr(browser, "is_cdp_connected", False):
                        pages = await browser.get_pages()
                        for p in pages:
                            try:
                                p_url = await p.get_url()
                                if not p_url or p_url.startswith("chrome://") or p_url.startswith("devtools://") or p_url.startswith("edge://") or p_url == "about:blank":
                                    continue
                                # Re-inject overlay if page reloaded or JS is uninitialized
                                res = await p.evaluate(
                                    "() => (!!document.getElementById('__omnitasker_panel') && window.__omnitasker_js_initialized === true)"
                                )
                                overlay_ready = (str(res).lower() == "true" or res is True)
                                if not overlay_ready:
                                    await self._orch._inject_overlay(p)
                                else:
                                    await self._orch._sync_overlay_state(p)
                                await self._orch._poll_overlay_commands(p)
                            except Exception:
                                pass
            except Exception:
                pass

            if self._orch.control_flags.get("stop_requested") or self._orch._stop_requested:
                self._orch._stop_requested = True
                self._orch._log("🛑 Stop signal detected. Cancelling current agent task...", "warn")
                if hasattr(self._orch, "_current_agent_task") and self._orch._current_agent_task and not self._orch._current_agent_task.done():
                    self._orch._current_agent_task.cancel()
                break

            await asyncio.sleep(0.5)


    async def _inject_overlay(self, page) -> None:
        """Read overlay_panel.html and inject it into the target page DOM."""
        if not hasattr(self._orch, "_injecting_pages") or self._orch._injecting_pages is None:
            self._orch._injecting_pages = set()
        page_id = getattr(page, "_target_id", None) or getattr(page, "target_id", None) or id(page)
        if page_id in self._orch._injecting_pages:
            return
        self._orch._injecting_pages.add(page_id)

        try:
            try:
                page_url = await page.get_url()
            except Exception:
                return

            if not page_url or page_url.startswith("chrome://") or page_url.startswith("devtools://") or page_url.startswith("edge://") or page_url == "about:blank":
                return

            overlay_path = Path(__file__).parent / "overlay_panel.html"
            if not overlay_path.exists():
                overlay_path = Path("d:/auto/overlay_panel.html")
            if not overlay_path.exists():
                return

            raw = overlay_path.read_text(encoding="utf-8")
            parts = raw.split("<!-- __OMNITASKER_JS__ -->")
            html_markup = parts[0].strip()
            js_code = parts[1].strip() if len(parts) > 1 else ""

            # Extract <style> block to inject both in stylesheet and inline
            css_content = ""
            style_match = re.search(r"<style>(.*?)</style>", html_markup, re.DOTALL | re.IGNORECASE)
            if style_match:
                css_content = style_match.group(1)

            # Check if panel already exists and JS is initialized
            try:
                check_res = await page.evaluate(
                    "() => JSON.stringify({ panel: !!document.getElementById('__omnitasker_panel'), js: !!window.__omnitasker_js_initialized })"
                )
                if isinstance(check_res, str):
                    try:
                        check_res = json.loads(check_res)
                    except Exception:
                        import ast
                        try:
                            check_res = ast.literal_eval(check_res)
                        except Exception:
                            check_res = {}
                panel_exists = check_res.get("panel") if isinstance(check_res, dict) else False
                js_ready = check_res.get("js") if isinstance(check_res, dict) else False
                if panel_exists:
                    if not js_ready and js_code:
                        try:
                            clean_js = js_code.strip()
                            if not (clean_js.startswith('(') and '=>' in clean_js):
                                clean_js = f"() => {{ {clean_js} }}"
                            await page.evaluate(clean_js)
                        except Exception:
                            pass
                    await self._orch._sync_overlay_state(page)
                    return
            except Exception:
                pass

            # Inject anti-debugger stealth bypass to neutralize Function("debugger")() and eval("debugger")
            try:
                await page.evaluate("""() => {
                    try {
                        const _Function = window.Function;
                        window.Function = function(...args) {
                            if (args.length > 0 && typeof args[args.length - 1] === 'string' && args[args.length - 1].includes('debugger')) {
                                return function() {};
                            }
                            return _Function.apply(this, args);
                        };
                        window.Function.prototype = _Function.prototype;
                    } catch(e) {}

                    try {
                        const _eval = window.eval;
                        window.eval = function(code) {
                            if (typeof code === 'string' && code.includes('debugger')) {
                                return;
                            }
                            return _eval.apply(this, arguments);
                        };
                    } catch(e) {}

                    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch(e) {}
                    try { if (window.chrome) { window.chrome.debugger = undefined; } } catch(e) {}
                }""")
            except Exception:
                pass



            # Atomic injection: inject CSS (with adoptedStyleSheets + nonce fallback), HTML (with TrustedTypes + DOMParser fallback)
            atomic_injection_script = """(payload) => {
                try {
                    const target = document.body || document.documentElement;
                    if (!target) return false;

                    // 1. Inject CSS bypassing CSP (Constructable Stylesheet + nonce-aware <style>)
                    if (payload.css) {
                        try {
                            if (window.CSSStyleSheet && document.adoptedStyleSheets) {
                                const sheet = new CSSStyleSheet();
                                sheet.replaceSync(payload.css);
                                document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
                            }
                        } catch(e) {}

                        if (!document.getElementById('__omnitasker_style')) {
                            try {
                                const style = document.createElement('style');
                                style.id = '__omnitasker_style';
                                const nonce = document.querySelector('style[nonce], script[nonce]')?.nonce;
                                if (nonce) style.nonce = nonce;
                                style.textContent = payload.css;
                                const head = document.head || document.documentElement || target;
                                head.appendChild(style);
                            } catch(e) {}
                        }
                    }

                    // 2. Inject HTML markup using TrustedTypes policy or DOMParser fallback
                    if (payload.html && !document.getElementById('__omnitasker_panel')) {
                        let htmlContent = payload.html;
                        let inserted = false;

                        if (window.trustedTypes) {
                            try {
                                if (!window.__ot_policy) {
                                    window.__ot_policy = window.trustedTypes.createPolicy('omnitasker', {
                                        createHTML: (s) => s,
                                        createScript: (s) => s
                                    });
                                }
                                if (window.__ot_policy) {
                                    htmlContent = window.__ot_policy.createHTML(payload.html);
                                }
                            } catch(e) {
                                try {
                                    if (window.trustedTypes.defaultPolicy) {
                                        htmlContent = window.trustedTypes.defaultPolicy.createHTML(payload.html);
                                    }
                                } catch(e2) {}
                            }
                        }

                        try {
                            target.insertAdjacentHTML('beforeend', htmlContent);
                            inserted = !!document.getElementById('__omnitasker_panel');
                        } catch(e) {}

                        // Fallback for strict Trusted Types: parse via DOMParser and append nodes directly
                        if (!inserted) {
                            try {
                                const parser = new DOMParser();
                                const doc = parser.parseFromString(payload.html, 'text/html');
                                // Use removeChild to move nodes from parsed doc to target (prevents infinite loop)
                                while (doc.head && doc.head.firstChild) target.appendChild(doc.head.removeChild(doc.head.firstChild));
                                while (doc.body && doc.body.firstChild) target.appendChild(doc.body.removeChild(doc.body.firstChild));
                            } catch(e) {}
                        }
                    }

                    const panel = document.getElementById('__omnitasker_panel');
                    if (panel) {
                        panel.style.setProperty('display', 'flex', 'important');
                        var mountTarget = document.documentElement || document.body;
                        if (mountTarget && panel.parentElement !== mountTarget) {
                            mountTarget.appendChild(panel);
                            const st = document.getElementById('__omnitasker_state');
                            const cmd = document.getElementById('__omnitasker_commands');
                            if (st && st.parentElement !== mountTarget) mountTarget.appendChild(st);
                            if (cmd && cmd.parentElement !== mountTarget) mountTarget.appendChild(cmd);
                        }
                        return true;
                    }
                    return false;
                } catch(err) {
                    console.error('OmniTasker overlay injection error:', err);
                    return false;
                }
            }"""

            res = await page.evaluate(
                atomic_injection_script,
                {"html": html_markup, "css": css_content}
            )

            exists = (str(res).lower() == "true" or res is True)

            # 3. Execute JS logic directly via CDP (immune to page CSP and Trusted Types restrictions!)
            if exists and js_code:
                try:
                    clean_js = js_code.strip()
                    if not (clean_js.startswith('(') and '=>' in clean_js):
                        clean_js = f"() => {{ {clean_js} }}"
                    await page.evaluate(clean_js)
                except Exception as js_err:
                    print(f"Overlay JS execution note: {js_err}")

            # Push initial state immediately
            if exists:
                await self._orch._sync_overlay_state(page)
                self._orch._log("💻 Overlay panel injected into browser.", "success")
        except Exception as e:
            print(f"Overlay injection warning: {e}")
        finally:
            if hasattr(self._orch, "_injecting_pages") and self._orch._injecting_pages is not None:
                self._orch._injecting_pages.discard(page_id)


    async def _sync_overlay_state(self, page) -> None:
        """Serialize current AgentState and push it to the hidden DOM element."""
        try:
            # Build a compact JSON payload for the overlay
            tasks_data = []
            total_tasks = 0
            if self._orch.state.task_queue and self._orch.state.task_queue.tasks:
                total_tasks = len(self._orch.state.task_queue.tasks)
                for t in self._orch.state.task_queue.tasks:
                    tasks_data.append({
                        "id": t.task_id,
                        "desc": t.description[:80],
                        "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
                    })

            logs_data = []
            for lg in self._orch.state.logs[-30:]:
                logs_data.append({
                    "t": lg.timestamp,
                    "l": lg.level,
                    "m": lg.message[:200],
                })

            payload = {
                "phase": self._orch.state.phase.value if hasattr(self._orch.state.phase, 'value') else str(self._orch.state.phase),
                "progress": self._orch.state.progress_pct,
                "currentTask": self._orch.state.current_task_index,
                "totalTasks": total_tasks,
                "thinking": (self._orch.state.model_thinking or "")[:300],
                "goal": (self._orch.state.model_next_goal or "")[:200],
                "memory": (self._orch.state.model_memory or "")[:200],
                "actions": self._orch.state.model_last_actions[:8],
                "logs": logs_data,
                "hitlReason": self._orch.state.hitl_reason,
                "failedCode": self._orch.state.failed_injection_code,
                "tasks": tasks_data,
            }

            payload_json = json.dumps(payload, ensure_ascii=True)

            sync_script = """(j) => {
                var el = document.getElementById('__omnitasker_state');
                if (el) el.dataset.state = j;
                var panel = document.getElementById('__omnitasker_panel');
                if (panel) {
                    if (panel.style.display === 'none') {
                        panel.style.setProperty('display', 'flex', 'important');
                    }
                    var mountTarget = document.documentElement || document.body;
                    if (mountTarget && panel.parentElement !== mountTarget) {
                        mountTarget.appendChild(panel);
                    }
                }
                // Direct UI update — don't wait for 750ms interval
                if (window.__ot_updateUI) {
                    try { window.__ot_updateUI(JSON.parse(j)); } catch(e) {}
                }
            }"""

            await page.evaluate(sync_script, payload_json)
        except Exception:
            pass  # Never crash the agent for overlay sync


    async def _poll_overlay_commands(self, page) -> None:
        """Read and dispatch user commands from the overlay's hidden element."""
        try:
            cmd_str = await page.evaluate(
                "() => {"
                "    var el = document.getElementById('__omnitasker_commands');"
                "    if (!el) return null;"
                "    var action = el.dataset.action || '';"
                "    var payload = el.dataset.payload || '';"
                "    if (!action) return null;"
                "    el.dataset.action = '';"
                "    el.dataset.payload = '';"
                "    return { action: action, payload: payload };"
                "}"
            )

            if not cmd_str:
                return

            if isinstance(cmd_str, dict):
                cmd = cmd_str
            else:
                import json
                try:
                    cmd = json.loads(cmd_str)
                except Exception:
                    return

            if not cmd or not isinstance(cmd, dict):
                return

            action = cmd.get("action", "")
            payload = cmd.get("payload", "")

            if action == "pause":
                import time
                self._orch.control_flags["pause_requested"] = True
                self._orch.control_flags["pause_click_time"] = time.time()
                self._orch._log("\u23F8\uFE0F Pause requested from browser overlay.", "warn")

            elif action == "resume":
                self._orch.hitl.resume()
                self._orch._log("\u25B6\uFE0F Resume triggered from browser overlay.", "success")

            elif action == "stop":
                self._orch.control_flags["stop_requested"] = True
                self._orch._stop_requested = True
                self._orch._log("\U0001f6d1 Stop requested from browser overlay.", "warn")

            elif action == "suggestion" and payload:
                if "suggestions" not in self._orch.control_flags:
                    self._orch.control_flags["suggestions"] = []
                self._orch.control_flags["suggestions"].append(payload)
                self._orch._log(f"\U0001f4ac Suggestion from overlay: {payload}", "success")

        except Exception:
            pass  # Never crash the agent for overlay polling


    async def _safe_sync_overlay(self) -> None:
        """Safely sync overlay state to the active browser page."""
        try:
            if not self._orch.browser_mgr.is_open:
                return
            browser = await self._orch.browser_mgr.get_browser()
            page = await self._orch._get_current_page_safe(browser)
            if page is not None:
                has_panel = False
                try:
                    res = await page.evaluate("() => !!document.getElementById('__omnitasker_panel')")
                    has_panel = (str(res).lower() == "true" or res is True)
                except Exception:
                    has_panel = False

                if not has_panel:
                    await self._orch._inject_overlay(page)
                else:
                    await self._orch._sync_overlay_state(page)
        except Exception:
            pass



    background_poll_loop = _background_poll_loop
    inject_overlay = _inject_overlay
    sync_overlay_state = _sync_overlay_state
    poll_overlay_commands = _poll_overlay_commands
    safe_sync_overlay = _safe_sync_overlay
