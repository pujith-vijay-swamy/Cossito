"""
devtools_perception.py — Chrome DevTools Protocol (CDP) Perception Engine (Phase 3).

Provides multimodal perception and telemetry via Chrome DevTools:
1. Accessibility Tree (AXTree) extraction for semantic role & node discovery.
2. Console Error & Runtime Exception capture.
3. Network Telemetry & HTTP failure tracking.
4. Unified MultimodalObservation snapshot generation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from models import (
    AccessibilitySnapshot,
    AXNode,
    ConsoleMessage,
    MultimodalObservation,
    NetworkRequestLog,
)

logger = logging.getLogger(__name__)


class DevToolsPerceptionEngine:
    """Multi-channel sensory perception engine interfacing with Chrome via CDP."""

    def __init__(self) -> None:
        self.console_logs: List[ConsoleMessage] = []
        self.network_logs: List[NetworkRequestLog] = []
        self._listeners_attached: set[str] = set()

    def clear_telemetry(self) -> None:
        """Reset accumulated console and network telemetry."""
        self.console_logs.clear()
        self.network_logs.clear()

    async def enable_cdp_domains(self, browser: Any, page: Any) -> bool:
        """Enable required CDP domains: Accessibility, Runtime, Network, and install telemetry hooks."""
        if browser is None or page is None:
            return False
        cdp_enabled = False
        try:
            cdp = getattr(browser, "_cdp_client_root", None)
            if cdp and hasattr(cdp, "send_raw"):
                sid = await page.session_id if hasattr(page, "session_id") else None
                if sid:
                    await cdp.send_raw("Runtime.enable", {}, session_id=sid)
                    await cdp.send_raw("Network.enable", {}, session_id=sid)
                    try:
                        await cdp.send_raw("Accessibility.enable", {}, session_id=sid)
                    except Exception:
                        pass
                    cdp_enabled = True
        except Exception as e:
            logger.debug(f"[DevToolsPerceptionEngine] enable_cdp_domains note: {e}")

        # Install in-page console & XHR/Fetch telemetry streaming hook
        try:
            await page.evaluate(r"""() => {
                if (window.__omnitasker_telemetry_installed) return;
                window.__omnitasker_telemetry_installed = true;
                window.__omnitasker_errors = window.__omnitasker_errors || [];
                window.__omnitasker_network_failures = window.__omnitasker_network_failures || [];

                const origError = console.error;
                console.error = function(...args) {
                    try {
                        window.__omnitasker_errors.push({
                            level: 'error',
                            text: args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ').slice(0, 500),
                            source: 'console.error'
                        });
                    } catch(e) {}
                    return origError.apply(console, args);
                };

                window.addEventListener('error', (ev) => {
                    window.__omnitasker_errors.push({
                        level: 'exception',
                        text: String(ev.message || ev.error || 'Runtime Error').slice(0, 500),
                        source: ev.filename || 'window.onerror',
                        lineno: ev.lineno || null
                    });
                });

                window.addEventListener('unhandledrejection', (ev) => {
                    window.__omnitasker_errors.push({
                        level: 'exception',
                        text: 'Unhandled Rejection: ' + String(ev.reason || '').slice(0, 500),
                        source: 'promise'
                    });
                });
            }""")
        except Exception:
            pass

        return cdp_enabled

    async def get_accessibility_tree(
        self,
        browser: Any,
        page: Any,
        max_depth: int = 5,
        max_nodes: int = 60,
    ) -> AccessibilitySnapshot:
        """Extract and structure the Chrome Accessibility (AX) Tree via CDP."""
        snapshot = AccessibilitySnapshot()
        if page is None:
            return snapshot

        # 1. Try native CDP Accessibility.getFullAXTree
        try:
            cdp = getattr(browser, "_cdp_client_root", None) if browser else None
            if cdp and hasattr(cdp, "send_raw"):
                sid = await page.session_id if hasattr(page, "session_id") else None
                if sid:
                    ax_data = await cdp.send_raw("Accessibility.getFullAXTree", {"depth": max_depth}, session_id=sid)
                    raw_nodes = ax_data.get("nodes", []) if isinstance(ax_data, dict) else []
                    
                    parsed_nodes: List[AXNode] = []
                    interactive_list: List[str] = []

                    for node in raw_nodes[:max_nodes]:
                        role_val = node.get("role", {}).get("value", "") if isinstance(node.get("role"), dict) else str(node.get("role", ""))
                        name_val = node.get("name", {}).get("value", "") if isinstance(node.get("name"), dict) else str(node.get("name", ""))
                        val_val = node.get("value", {}).get("value", "") if isinstance(node.get("value"), dict) else str(node.get("value", ""))
                        desc_val = node.get("description", {}).get("value", "") if isinstance(node.get("description"), dict) else str(node.get("description", ""))

                        disabled = False
                        focused = False
                        for prop in node.get("properties", []):
                            pname = prop.get("name")
                            pval = prop.get("value", {}).get("value")
                            if pname == "disabled" and pval:
                                disabled = True
                            elif pname == "focused" and pval:
                                focused = True

                        ax_node = AXNode(
                            node_id=str(node.get("nodeId", "")),
                            role=role_val,
                            name=name_val,
                            value=val_val,
                            description=desc_val,
                            disabled=disabled,
                            focused=focused,
                            children_ids=[str(c) for c in node.get("childIds", [])],
                        )
                        parsed_nodes.append(ax_node)

                        if role_val in ("button", "link", "textbox", "tab", "combobox", "checkbox", "menuitem") and name_val:
                            interactive_list.append(f"[{role_val.upper()}] '{name_val}'" + (" (disabled)" if disabled else ""))

                    if parsed_nodes:
                        snapshot.nodes = parsed_nodes
                        snapshot.interactive_elements = interactive_list
                        return snapshot
        except Exception as cdp_err:
            logger.debug(f"[DevToolsPerceptionEngine] CDP AXTree extraction note: {cdp_err}")

        # 2. DOM-based accessibility & computed-style heuristic fallback
        try:
            fallback_data = await page.evaluate(r"""() => {
                const nodes = [];
                const interactive = [];
                const elems = Array.from(document.querySelectorAll(
                    'button, [role="button"], a[href], input, textarea, select, [role="tab"], [role="menuitem"], [contenteditable="true"], .monaco-editor, .CodeMirror, .ace_editor, .cm-editor'
                )).slice(0, 50);

                for (let i = 0; i < elems.length; i++) {
                    const el = elems[i];
                    if (el.offsetParent === null) continue;
                    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
                    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) continue;
                    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                    const bounds = rect ? [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)] : [];
                    const role = el.getAttribute('role') || el.tagName.toLowerCase();
                    const name = el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('title') || '';
                    const cleanName = name.replace(/\s+/g, ' ').trim().slice(0, 60);
                    const disabled = el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true';
                    const focused = document.activeElement === el;

                    nodes.push({
                        node_id: String(i + 1),
                        role: role,
                        name: cleanName,
                        value: el.value || '',
                        description: el.getAttribute('aria-description') || '',
                        disabled: disabled,
                        focused: focused,
                        bounds: bounds,
                        children_ids: []
                    });

                    if (cleanName) {
                        interactive.push(`[${role.toUpperCase()}] '${cleanName}'` + (disabled ? ' (disabled)' : ''));
                    }
                }
                return { nodes: nodes, interactive_elements: interactive };
            }""")
            from dom_extraction import parse_eval_result
            fallback_data = parse_eval_result(fallback_data)
            if isinstance(fallback_data, dict):
                snapshot.nodes = [AXNode(**n) for n in fallback_data.get("nodes", [])]
                snapshot.interactive_elements = fallback_data.get("interactive_elements", [])
        except Exception as fb_err:
            logger.debug(f"[DevToolsPerceptionEngine] DOM AX fallback note: {fb_err}")

        return snapshot

    async def get_console_errors(self, page: Any) -> List[ConsoleMessage]:
        """Query recent runtime console errors, warnings, and uncaught exceptions from page."""
        if page is None:
            return []
        
        errors: List[ConsoleMessage] = []
        try:
            # Query window errors collected in page context
            page_errors = await page.evaluate(r"""() => {
                const results = [];
                if (window.__omnitasker_errors && Array.isArray(window.__omnitasker_errors)) {
                    for (const err of window.__omnitasker_errors) {
                        results.push({
                            level: err.level || 'error',
                            text: String(err.message || err.text || err),
                            source: err.source || 'page_runtime',
                            line_number: err.lineno || null
                        });
                    }
                }
                return results;
            }""")
            from dom_extraction import parse_eval_result
            page_errors = parse_eval_result(page_errors)
            if isinstance(page_errors, list):
                for e in page_errors:
                    if isinstance(e, dict):
                        txt = e.get("text") or e.get("message") or ""
                        errors.append(ConsoleMessage(
                            level=e.get("level", "error"),
                            text=str(txt),
                            source=e.get("source", "page_runtime"),
                            line_number=e.get("line_number") or e.get("lineno"),
                        ))
                    elif isinstance(e, ConsoleMessage):
                        errors.append(e)
        except Exception:
            pass

        # Merge with internally recorded console logs
        for log in self.console_logs:
            if log.level in ("error", "exception"):
                errors.append(log)

        return errors

    async def get_network_failures(self, page: Any) -> List[NetworkRequestLog]:
        """Query failed network requests, 4xx/5xx responses, and connection drops."""
        if page is None:
            return []

        failures: List[NetworkRequestLog] = []
        try:
            page_failures = await page.evaluate(r"""() => {
                const results = [];
                if (window.performance && typeof window.performance.getEntriesByType === 'function') {
                    const resEntries = window.performance.getEntriesByType('resource');
                    for (const entry of resEntries) {
                        // Check for 0 duration or failed load indicators if present
                        if (entry.responseStatus && entry.responseStatus >= 400) {
                            results.push({
                                url: entry.name,
                                method: 'GET',
                                status_code: entry.responseStatus,
                                resource_type: entry.initiatorType || 'resource',
                                failed: true,
                                error_text: `HTTP Status ${entry.responseStatus}`
                            });
                        }
                    }
                }
                return results.slice(-20);
            }""")
            from dom_extraction import parse_eval_result
            page_failures = parse_eval_result(page_failures)
            if isinstance(page_failures, list):
                for f in page_failures:
                    if isinstance(f, dict):
                        failures.append(NetworkRequestLog(
                            url=f.get("url") or f.get("name") or "",
                            method=f.get("method", "GET"),
                            status_code=f.get("status_code") or f.get("responseStatus"),
                            resource_type=f.get("resource_type") or f.get("initiatorType") or "resource",
                            failed=f.get("failed", True),
                            error_text=f.get("error_text"),
                        ))
                    elif isinstance(f, NetworkRequestLog):
                        failures.append(f)
        except Exception:
            pass

        for nlog in self.network_logs:
            if nlog.failed or (nlog.status_code and nlog.status_code >= 400):
                failures.append(nlog)

        return failures

    async def capture_multimodal_observation(
        self,
        browser: Any,
        page: Any,
        include_screenshot: bool = False,
    ) -> MultimodalObservation:
        """Assemble a unified multimodal observation combining DOM, AXTree, Console, and Network telemetry."""
        current_url = ""
        dom_summary = ""

        if page is not None:
            try:
                current_url = await page.get_url()
            except Exception:
                current_url = ""

            try:
                dom_summary = await page.evaluate(r"""() => {
                    const title = document.title || '';
                    const mainText = (document.body ? document.body.innerText : '').slice(0, 1500).replace(/\s+/g, ' ');
                    return `Title: ${title} | Body Preview: ${mainText}`;
                }""")
            except Exception:
                dom_summary = ""

        a11y_tree = await self.get_accessibility_tree(browser, page)
        console_errs = await self.get_console_errors(page)
        net_fails = await self.get_network_failures(page)

        return MultimodalObservation(
            url=current_url,
            dom_summary=dom_summary,
            a11y_tree=a11y_tree,
            console_errors=console_errs,
            network_failures=net_fails,
            has_screenshot=include_screenshot,
        )
