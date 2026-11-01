"""
Module for interacting with various code editors in browser automation.
Implements safe editor interaction with snapshot-before-write, readback verification,
and multi-editor support.
"""
from __future__ import annotations

import asyncio
import logging
import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Any, Tuple, Dict, List

from models import WorkspaceSnapshot

logger = logging.getLogger(__name__)

# A JS helper to find elements across same-origin iframes.
# Evaluates a function `fn(win)` on the main window and all reachable same-origin iframes.
JS_RECURSIVE_SEARCH = """
function searchIframes(fn, win) {
    win = win || window;
    try {
        let result = fn(win);
        if (result) return result;
    } catch (e) {}
    try {
        const iframes = win.document ? win.document.querySelectorAll('iframe') : [];
        for (let i = 0; i < iframes.length; i++) {
            try {
                const iframeWin = iframes[i].contentWindow;
                if (iframeWin) {
                    let res = searchIframes(fn, iframeWin);
                    if (res) return res;
                }
            } catch (e) {}
        }
    } catch (e) {}
    try {
        for (let i = 0; i < win.frames.length; i++) {
            try {
                let frame = win.frames[i];
                if (frame && frame.location) {
                    let res = searchIframes(fn, frame);
                    if (res) return res;
                }
            } catch (e) {}
        }
    } catch (e) {}
    return null;
}
"""

JS_MULTI_EDITOR_HELPERS = r"""
function normalizeTarget(t) { return String(t || '').toLowerCase().trim(); }
function matchesTargetKeywords(text, target) {
    const value = normalizeTarget(text), t = normalizeTarget(target);
    if (!value || !t) return false;
    if (value === t) return true;
    const tokens = value.split(/[^a-z0-9]+/);
    const aliases = {html:['html','htm','htmlmixed','xml'], css:['css','stylesheet'],
                     javascript:['javascript','js'], js:['javascript','js']};
    return (aliases[t] || []).some(alias => tokens.includes(alias));
}
function editorEvidence(el, doc) {
    const evidence = [];
    const selector = '.monaco-editor, .CodeMirror, .cm-editor, .ace_editor, textarea, [contenteditable="true"]';
    for (let node = el; node && node !== doc.body; node = node.parentElement) {
        // Never borrow a sibling editor's heading from a common container.
        if (node !== el && Array.from(node.querySelectorAll(selector)).filter(candidate =>
            !candidate.parentElement?.closest(selector)).length > 1) break;
        for (const key of ['id','name','aria-label','data-lang','data-language','data-mode','data-filename']) {
            evidence.push(node.getAttribute(key) || '');
        }
        for (const id of (node.getAttribute('aria-labelledby') || '').split(/\s+/)) {
            evidence.push(doc.getElementById(id)?.textContent || '');
        }
        const heading = node.querySelector('label,h1,h2,h3,h4,h5,h6,.filename');
        if (heading) evidence.push(heading.textContent || '');
    }
    try {
        const cm = el.CodeMirror;
        if (cm) { const mode = cm.getOption('mode'); evidence.push(typeof mode === 'string' ? mode : mode?.name || ''); }
        const ace = el.env?.editor;
        if (ace) evidence.push(ace.getSession().getMode().$id);
    } catch (_) {}
    return evidence;
}
function editorLanguage(el, doc) {
    const evidence = editorEvidence(el, doc);
    const matches = ['html','css','javascript'].filter(lang => evidence.some(e => matchesTargetKeywords(e, lang)));
    return matches.length === 1 ? matches[0] : 'unknown';
}
function findEditorElementForTarget(elements, target, doc) {
    if (!elements.length) return null;
    if (!target) return elements.find(el => el.getClientRects().length) || elements[0];
    const key = normalizeTarget(target);
    const indexed = /^editor_(\d+)$/.exec(key);
    if (indexed) return elements[Number(indexed[1])] || null;
    const matches = elements.filter(el => editorEvidence(el, doc).some(e => matchesTargetKeywords(e, key)));
    return matches.length === 1 ? matches[0] : null;
}
"""


def classify_code_language(code: str) -> str:
    """Classify whether code is 'html', 'css', 'javascript', or 'unknown'."""
    if not code or not code.strip():
        return "unknown"
    stripped = code.strip()
    code_lower = stripped.lower()

    html_markers = [
        "<!doctype html", "<html", "<head", "<body", "<div", "<span", "<p>",
        "<h1", "<h2", "<h3", "<h4", "<section", "<header", "<footer", "<main",
        "<table", "<form", "<link", "<meta", "<!--"
    ]
    has_html_tags = any(m in code_lower for m in html_markers)

    css_properties = [
        "margin:", "padding:", "color:", "background:", "background-color:",
        "font-size:", "font-family:", "display:", "flex:", "grid:", "border:",
        "width:", "height:", "align-items:", "justify-content:", "box-sizing:",
        "text-align:", "border-radius:", "position:", "top:", "left:", "right:",
        "bottom:", "z-index:", "overflow:", "@media", "@keyframes", ":hover",
        ":root", "line-height:", "letter-spacing:", "cursor:"
    ]
    has_css_props = sum(1 for prop in css_properties if prop in code_lower)
    has_braces = "{" in code and "}" in code

    if has_html_tags:
        return "html"
    if (has_css_props >= 2 or (has_braces and has_css_props >= 1)) and not has_html_tags:
        return "css"

    js_markers = ["function", "const ", "let ", "var ", "console.log", "addeventlistener", "document.getelement"]
    if any(m in code_lower for m in js_markers) and not has_html_tags:
        return "javascript"

    return "unknown"

def split_multi_editor_code(code: str) -> Dict[str, str]:
    """Detect and split code that contains multiple language blocks (e.g. HTML and CSS)."""
    if not code or not code.strip():
        return {}

    result = {}

    # 1. Check for markdown fenced code blocks: ```html ... ``` and ```css ... ```
    html_blocks = re.findall(r'```(?:html|htm)\s*\n([\s\S]*?)```', code, re.IGNORECASE)
    css_blocks = re.findall(r'```(?:css)\s*\n([\s\S]*?)```', code, re.IGNORECASE)
    js_blocks = re.findall(r'```(?:javascript|js)\s*\n([\s\S]*?)```', code, re.IGNORECASE)

    if html_blocks or css_blocks or js_blocks:
        for language, blocks in (("html", html_blocks), ("css", css_blocks), ("javascript", js_blocks)):
            if blocks:
                result[language] = "\n\n".join(b.strip() for b in blocks)
        return result

    # 2. Check for explicit comment / header sections
    sections = re.split(
        r'(?i)(?:<!--\s*(?:html|index\.html)\s*-->|/\*\s*(?:css|style\.css|styles\.css)\s*\*/|(?:\bindex\.html\b|\bstyle\.css\b|\bstyles\.css\b)\s*:)',
        code
    )
    if len(sections) > 1:
        delims = re.findall(
            r'(?i)(?:<!--\s*(html|index\.html)\s*-->|/\*\s*(css|style\.css|styles\.css)\s*\*/|(\bindex\.html\b|\bstyle\.css\b|\bstyles\.css\b)\s*:)',
            code
        )
        if len(delims) >= 2:
            for delim_tuple, content in zip(delims, sections[1:]):
                tag = next(t for t in delim_tuple if t).lower()
                lang = "html" if ("html" in tag) else "css" if ("css" in tag) else "unknown"
                if lang != "unknown" and content.strip():
                    result[lang] = content.strip()
            if "html" in result and "css" in result:
                return result

    # 3. Check for HTML containing an embedded <style> block
    style_matches = re.findall(r'<style[^>]*>([\s\S]*?)</style>', code, re.IGNORECASE)
    if style_matches:
        extracted_css = "\n\n".join(s.strip() for s in style_matches if s.strip())
        if classify_code_language(extracted_css) == "css":
            cleaned_html = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', code, flags=re.IGNORECASE).strip()
            if classify_code_language(cleaned_html) == "html":
                result["html"] = cleaned_html
                result["css"] = extracted_css
                return result

    single_lang = classify_code_language(code)
    if single_lang != "unknown":
        result[single_lang] = code.strip()
    return result


class EditorAdapter(ABC):
    """Abstract base class for editor adapters."""

    @abstractmethod
    async def detect(self, page: Any) -> bool:
        """Detect if this editor type is present on the page."""
        pass

    @abstractmethod
    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        """Capture the current state of the editor."""
        pass

    @abstractmethod
    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        """Write code to the editor."""
        pass

    @abstractmethod
    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        """Read the current content of the editor."""
        pass

    @abstractmethod
    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        """Detect the current language of the editor."""
        pass

    @abstractmethod
    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        """Get the editor model identifier."""
        pass


class MonacoAdapter(EditorAdapter):
    """Adapter for Monaco Editor."""
    async def detect(self, page: Any) -> bool:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    if (win.monaco.editor.getModels().length > 0) return true;
                    if (win.monaco.editor.getEditors && win.monaco.editor.getEditors().length > 0) return true;
                }}
                if (win.document && win.document.querySelector('.monaco-editor')) return true;
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        content = await self.readback(page, target=target)
        lang = await self.get_language(page, target=target)
        model_id = await self.get_model_id(page, target=target)
        snap = WorkspaceSnapshot(
            editor_kind="monaco",
            content=str(content or ""),
            language=str(lang) if isinstance(lang, str) else None,
            model_id=str(model_id) if isinstance(model_id, str) else None,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        if not target:
            script = f"""(code) => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    // 1. Standard win.monaco API
                    if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                        if (win.monaco.editor.getEditors) {{
                            const eds = win.monaco.editor.getEditors();
                            for (const ed of eds) {{
                                try {{
                                    if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {{
                                        ed.setValue(code);
                                        return true;
                                    }}
                                }} catch(e) {{}}
                            }}
                            if (eds.length > 0 && eds[0].getModel()) {{
                                eds[0].getModel().setValue(code);
                                return true;
                            }}
                        }}
                        const models = win.monaco.editor.getModels();
                        if (models.length > 0) {{
                            models[0].setValue(code);
                            return true;
                        }}
                    }}
                    // 2. Custom window editor properties
                    for (const k of ['editor', 'monacoEditor', 'codeEditor']) {{
                        try {{
                            if (win[k] && typeof win[k].setValue === 'function') {{
                                win[k].setValue(code);
                                return true;
                            }}
                        }} catch(e) {{}}
                    }}
                    // 3. Bundled Monaco DOM fallback via inputarea
                    if (win.document) {{
                        const monacoEls = Array.from(win.document.querySelectorAll('.monaco-editor'));
                        for (const mEl of monacoEls) {{
                            if (mEl.offsetParent !== null || monacoEls.length === 1) {{
                                const ta = mEl.querySelector('textarea.inputarea');
                                if (ta) {{
                                    try {{
                                        ta.focus();
                                        win.document.execCommand('selectAll', false, null);
                                        if (win.document.execCommand('insertText', false, code)) return true;
                                        const dt = new DataTransfer();
                                        dt.setData('text/plain', code);
                                        const pasteEv = new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }});
                                        ta.dispatchEvent(pasteEv);
                                        return true;
                                    }} catch(e) {{}}
                                }}
                            }}
                        }}
                    }}
                    return false;
                }}) || false;
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script, code), timeout=10.0)
                return str(res).lower() == "true" or res is True
            except Exception:
                return False

        script = f"""({{code, target}}) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                const targetLower = normalizeTarget(target);
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels ? win.monaco.editor.getModels() : [];
                    const eds = win.monaco.editor.getEditors ? win.monaco.editor.getEditors() : [];
                    const validModels = models.filter(m => !m.uri || !m.uri.path.endsWith('.d.ts'));

                    if (targetLower) {{
                        const matchingModels = validModels.filter(m => {{
                            const lang = (m.getLanguageId ? m.getLanguageId() : '').toLowerCase();
                            const path = (m.uri ? (m.uri.path || m.uri.toString()) : '').toLowerCase();
                            return matchesTargetKeywords(lang, targetLower) || matchesTargetKeywords(path, targetLower);
                        }});
                        const matchedModel = matchingModels.length === 1 ? matchingModels[0] : null;
                        if (matchedModel) {{
                            matchedModel.setValue(code);
                            return true;
                        }}

                    }}

                    if (targetLower) return false;

                    if (eds.length > 0) {{
                        for (const ed of eds) {{
                            try {{
                                if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {{
                                    ed.setValue(code);
                                    return true;
                                }}
                            }} catch(e) {{}}
                        }}
                        if (eds[0].getModel()) {{
                            eds[0].getModel().setValue(code);
                            return true;
                        }}
                    }}
                    if (models.length > 0) {{
                        models[0].setValue(code);
                        return true;
                    }}
                }}
                if (win.document) {{
                    const monacoEls = Array.from(win.document.querySelectorAll('.monaco-editor'));
                    if (monacoEls.length > 0) {{
                        const targetEl = findEditorElementForTarget(monacoEls, targetLower, win.document);
                        if (targetEl) {{
                            const ta = targetEl.querySelector('textarea.inputarea');
                            if (ta) {{
                                try {{
                                    ta.focus();
                                    win.document.execCommand('selectAll', false, null);
                                    if (win.document.execCommand('insertText', false, code)) return true;
                                    const dt = new DataTransfer();
                                    dt.setData('text/plain', code);
                                    const pasteEv = new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }});
                                    ta.dispatchEvent(pasteEv);
                                    return true;
                                }} catch(e) {{}}
                            }}
                        }}
                    }}
                }}
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, {"code": code, "target": target}), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        if not target:
            script = f"""() => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                        if (win.monaco.editor.getEditors) {{
                            const eds = win.monaco.editor.getEditors();
                            for (const ed of eds) {{
                                try {{
                                    if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {{
                                        return ed.getValue();
                                    }}
                                }} catch(e) {{}}
                            }}
                            if (eds.length > 0 && eds[0].getModel()) {{
                                return eds[0].getModel().getValue();
                            }}
                        }}
                        const models = win.monaco.editor.getModels();
                        if (models.length > 0) return models[0].getValue();
                    }}
                    for (const k of ['editor', 'monacoEditor', 'codeEditor']) {{
                        try {{
                            if (win[k] && typeof win[k].getValue === 'function') return win[k].getValue();
                        }} catch(e) {{}}
                    }}
                    if (win.document) {{
                        const monacoEls = Array.from(win.document.querySelectorAll('.monaco-editor'));
                        for (const mEl of monacoEls) {{
                            if (mEl.offsetParent !== null || monacoEls.length === 1) {{
                                const vl = mEl.querySelector('.view-lines');
                                if (vl) {{
                                    const lines = Array.from(vl.querySelectorAll('.view-line')).map(l => l.textContent);
                                    if (lines.length > 0) return lines.join('\\n');
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}) || "";
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
                return str(res) if (res is not None and res is not False) else ""
            except Exception:
                return ""

        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                const targetLower = normalizeTarget(target);
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels ? win.monaco.editor.getModels() : [];
                    const eds = win.monaco.editor.getEditors ? win.monaco.editor.getEditors() : [];
                    const validModels = models.filter(m => !m.uri || !m.uri.path.endsWith('.d.ts'));

                    if (targetLower && validModels.length > 0) {{
                        const matches = validModels.filter(m => {{
                            const lang = (m.getLanguageId ? m.getLanguageId() : '').toLowerCase();
                            const path = (m.uri ? (m.uri.path || m.uri.toString()) : '').toLowerCase();
                            return matchesTargetKeywords(lang, targetLower) || matchesTargetKeywords(path, targetLower);
                        }});
                        if (matches.length === 1) return matches[0].getValue();
                        return null;
                    }}

                    if (eds.length > 0) {{
                        for (const ed of eds) {{
                            try {{
                                if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {{
                                    return ed.getValue();
                                }}
                            }} catch(e) {{}}
                        }}
                        if (eds[0].getModel()) {{
                            return eds[0].getModel().getValue();
                        }}
                    }}
                    if (models.length > 0) return models[0].getValue();
                }}
                if (win.document) {{
                    const monacoEls = Array.from(win.document.querySelectorAll('.monaco-editor'));
                    const targetEl = findEditorElementForTarget(monacoEls, targetLower, win.document);
                    if (targetEl) {{
                        const vl = targetEl.querySelector('.view-lines');
                        if (vl) {{
                            const lines = Array.from(vl.querySelectorAll('.view-line')).map(l => l.textContent);
                            if (lines.length > 0) return lines.join('\\n');
                        }}
                    }}
                }}
                return null;
            }}) || "";
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if (res is not None and res is not False) else ""
        except Exception:
            return ""

    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels().filter(m => !target ||
                        matchesTargetKeywords(m.getLanguageId?.(), target) || matchesTargetKeywords(m.uri?.path || m.uri?.toString(), target));
                    if (models.length > 0 && models[0].getLanguageId) return models[0].getLanguageId();
                }}
                return null;
            }});
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if res else None
        except Exception:
            return None

    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels().filter(m => !target ||
                        matchesTargetKeywords(m.getLanguageId?.(), target) || matchesTargetKeywords(m.uri?.path || m.uri?.toString(), target));
                    if (models.length > 0 && models[0].uri) return models[0].uri.toString();
                }}
                return null;
            }});
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if res else None
        except Exception:
            return None


class CodeMirror5Adapter(EditorAdapter):
    """Adapter for CodeMirror 5."""
    async def detect(self, page: Any) -> bool:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const el = win.document.querySelector('.CodeMirror');
                if (el && el.CodeMirror) return true;
                if (el) return true;
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        content = await self.readback(page, target=target)
        lang = await self.get_language(page, target=target)
        snap = WorkspaceSnapshot(
            editor_kind="codemirror5",
            content=content,
            language=lang,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        if not target:
            script = f"""(code) => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return false;
                    const elems = Array.from(win.document.querySelectorAll('.CodeMirror'));
                    if (elems.length === 0) return false;
                    elems.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of elems) {{
                        if (el.CodeMirror) {{
                            el.CodeMirror.setValue(code);
                            if (el.CodeMirror.save) el.CodeMirror.save();
                            return true;
                        }}
                    }}
                    return false;
                }}) || false;
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script, code), timeout=10.0)
                return str(res).lower() == "true" or res is True
            except Exception:
                return False

        script = f"""({{code, target}}) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const elems = Array.from(win.document.querySelectorAll('.CodeMirror'));
                if (elems.length === 0) return false;
                const targetLower = normalizeTarget(target);
                const targetEl = findEditorElementForTarget(elems, targetLower, win.document);
                if (targetEl && targetEl.CodeMirror) {{
                    targetEl.CodeMirror.setValue(code);
                    if (targetEl.CodeMirror.save) targetEl.CodeMirror.save();
                    return true;
                }}
                if (targetLower) return false;
                elems.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                for (const el of elems) {{
                    if (el.CodeMirror) {{
                        el.CodeMirror.setValue(code);
                        if (el.CodeMirror.save) el.CodeMirror.save();
                        return true;
                    }}
                }}
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, {"code": code, "target": target}), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        if not target:
            script = f"""() => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return null;
                    const elems = Array.from(win.document.querySelectorAll('.CodeMirror'));
                    if (elems.length === 0) return null;
                    elems.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of elems) {{
                        if (el.CodeMirror) return el.CodeMirror.getValue();
                    }}
                    return null;
                }}) || "";
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
                return str(res) if (res is not None and res is not False) else ""
            except Exception:
                return ""

        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const elems = Array.from(win.document.querySelectorAll('.CodeMirror'));
                if (elems.length === 0) return null;
                const targetLower = normalizeTarget(target);
                const targetEl = findEditorElementForTarget(elems, targetLower, win.document);
                if (targetEl && targetEl.CodeMirror) {{
                    return targetEl.CodeMirror.getValue();
                }}
                if (targetLower) return false;
                elems.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                if (targetLower) return null;
                for (const el of elems) {{
                    if (el.CodeMirror) return el.CodeMirror.getValue();
                }}
                return null;
            }}) || "";
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if (res is not None and res is not False) else ""
        except Exception:
            return ""

    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const el = win.document.querySelector('.CodeMirror');
                if (el && el.CodeMirror) {{
                    const mode = el.CodeMirror.getOption('mode');
                    return typeof mode === 'string' ? mode : (mode && mode.name ? mode.name : null);
                }}
                return null;
            }});
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res) if res else None
        except Exception:
            return None

    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None


class CodeMirror6Adapter(EditorAdapter):
    """Adapter for CodeMirror 6."""
    async def detect(self, page: Any) -> bool:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return false;
                if (win.document.querySelector('.cm-content, .cm-editor')) return true;
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        content = await self.readback(page, target=target)
        snap = WorkspaceSnapshot(
            editor_kind="codemirror6",
            content=content,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        if not target:
            script = f"""(code) => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return false;
                    const cmEditors = Array.from(win.document.querySelectorAll('.cm-editor'));
                    cmEditors.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const cmEditor of cmEditors) {{
                        const view = (cmEditor.cmView && (cmEditor.cmView.view || (typeof cmEditor.cmView.dispatch === 'function' ? cmEditor.cmView : null)));
                        if (view && typeof view.dispatch === 'function') {{
                            view.dispatch({{
                                changes: {{ from: 0, to: view.state.doc.length, insert: code }}
                            }});
                            return true;
                        }}
                    }}
                    const cmContents = Array.from(win.document.querySelectorAll('.cm-content'));
                    cmContents.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of cmContents) {{
                        const view = (el.cmView && (el.cmView.view || (typeof el.cmView.dispatch === 'function' ? el.cmView : null)));
                        if (view && typeof view.dispatch === 'function') {{
                            view.dispatch({{
                                changes: {{ from: 0, to: view.state.doc.length, insert: code }}
                            }});
                            return true;
                        }}
                        try {{
                            el.focus();
                            const sel = win.getSelection();
                            const range = win.document.createRange();
                            range.selectNodeContents(el);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            if (win.document.execCommand('insertText', false, code)) return true;
                            const dt = new DataTransfer();
                            dt.setData('text/plain', code);
                            const pasteEv = new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }});
                            el.dispatchEvent(pasteEv);
                            return true;
                        }} catch (e) {{}}
                    }}
                    return false;
                }}) || false;
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script, code), timeout=10.0)
                return str(res).lower() == "true" or res is True
            except Exception:
                return False

        script = f"""({{code, target}}) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const cmEditors = Array.from(win.document.querySelectorAll('.cm-editor'));
                const targetLower = normalizeTarget(target);
                if (cmEditors.length > 0) {{
                    const targetEl = findEditorElementForTarget(cmEditors, targetLower, win.document);
                    if (targetEl) {{
                        const view = (targetEl.cmView && (targetEl.cmView.view || (typeof targetEl.cmView.dispatch === 'function' ? targetEl.cmView : null)));
                        if (view && typeof view.dispatch === 'function') {{
                            view.dispatch({{
                                changes: {{ from: 0, to: view.state.doc.length, insert: code }}
                            }});
                            return true;
                        }}
                    }}
                }}
                if (targetLower) return false;
                cmEditors.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                for (const cmEditor of cmEditors) {{
                    const view = (cmEditor.cmView && (cmEditor.cmView.view || (typeof cmEditor.cmView.dispatch === 'function' ? cmEditor.cmView : null)));
                    if (view && typeof view.dispatch === 'function') {{
                        view.dispatch({{
                            changes: {{ from: 0, to: view.state.doc.length, insert: code }}
                        }});
                        return true;
                    }}
                }}
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, {"code": code, "target": target}), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        if not target:
            script = f"""() => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return null;
                    const cmEditors = Array.from(win.document.querySelectorAll('.cm-editor'));
                    cmEditors.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const cmEditor of cmEditors) {{
                        const view = (cmEditor.cmView && (cmEditor.cmView.view || (typeof cmEditor.cmView.dispatch === 'function' ? cmEditor.cmView : null)));
                        if (view && view.state && view.state.doc) {{
                            return view.state.doc.toString();
                        }}
                    }}
                    const cmContents = Array.from(win.document.querySelectorAll('.cm-content'));
                    cmContents.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of cmContents) {{
                        const view = (el.cmView && (el.cmView.view || (typeof el.cmView.dispatch === 'function' ? el.cmView : null)));
                        if (view && view.state && view.state.doc) {{
                            return view.state.doc.toString();
                        }}
                        const lines = el.querySelectorAll('.cm-line');
                        if (lines.length > 0) {{
                            return Array.from(lines).map(l => l.textContent).join('\\n');
                        }}
                        return el.textContent;
                    }}
                    return null;
                }}) || "";
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
                return str(res) if (res is not None and res is not False) else ""
            except Exception:
                return ""

        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const cmEditors = Array.from(win.document.querySelectorAll('.cm-editor'));
                const targetLower = normalizeTarget(target);
                if (cmEditors.length > 0) {{
                    const targetEl = findEditorElementForTarget(cmEditors, targetLower, win.document);
                    if (targetEl) {{
                        const view = (targetEl.cmView && (targetEl.cmView.view || (typeof targetEl.cmView.dispatch === 'function' ? targetEl.cmView : null)));
                        if (view && view.state && view.state.doc) {{
                            return view.state.doc.toString();
                        }}
                    }}
                }}
                if (targetLower) return false;
                cmEditors.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                if (targetLower) return null;
                for (const cmEditor of cmEditors) {{
                    const view = (cmEditor.cmView && (cmEditor.cmView.view || (typeof cmEditor.cmView.dispatch === 'function' ? cmEditor.cmView : null)));
                    if (view && view.state && view.state.doc) {{
                        return view.state.doc.toString();
                    }}
                }}
                return null;
            }}) || "";
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if (res is not None and res is not False) else ""
        except Exception:
            return ""

    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None

    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None


class AceAdapter(EditorAdapter):
    """Adapter for Ace Editor."""
    async def detect(self, page: Any) -> bool:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return false;
                if (win.document.querySelector('.ace_editor') || typeof win.ace !== 'undefined') return true;
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        content = await self.readback(page, target=target)
        lang = await self.get_language(page, target=target)
        snap = WorkspaceSnapshot(
            editor_kind="ace",
            content=content,
            language=lang,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        if not target:
            script = f"""(code) => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return false;
                    const elems = Array.from(win.document.querySelectorAll('.ace_editor'));
                    elems.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of elems) {{
                        if (typeof win.ace !== 'undefined' && win.ace.edit) {{
                            win.ace.edit(el).setValue(code, -1);
                            return true;
                        }}
                        if (el.env && el.env.editor) {{
                            el.env.editor.setValue(code, -1);
                            return true;
                        }}
                    }}
                    return false;
                }}) || false;
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script, code), timeout=10.0)
                return str(res).lower() == "true" or res is True
            except Exception:
                return False

        script = f"""({{code, target}}) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const elems = Array.from(win.document.querySelectorAll('.ace_editor'));
                const targetLower = normalizeTarget(target);
                const targetEl = findEditorElementForTarget(elems, targetLower, win.document);
                if (targetEl) {{
                    if (typeof win.ace !== 'undefined' && win.ace.edit) {{
                        win.ace.edit(targetEl).setValue(code, -1);
                        return true;
                    }}
                    if (targetEl.env && targetEl.env.editor) {{
                        targetEl.env.editor.setValue(code, -1);
                        return true;
                    }}
                }}
                if (targetLower) return false;
                elems.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                for (const el of elems) {{
                    if (typeof win.ace !== 'undefined' && win.ace.edit) {{
                        win.ace.edit(el).setValue(code, -1);
                        return true;
                    }}
                    if (el.env && el.env.editor) {{
                        el.env.editor.setValue(code, -1);
                        return true;
                    }}
                }}
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, {"code": code, "target": target}), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        if not target:
            script = f"""() => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return null;
                    const elems = Array.from(win.document.querySelectorAll('.ace_editor'));
                    elems.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    for (const el of elems) {{
                        if (typeof win.ace !== 'undefined' && win.ace.edit) return win.ace.edit(el).getValue();
                        if (el.env && el.env.editor) return el.env.editor.getValue();
                    }}
                    return null;
                }}) || "";
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
                return str(res) if (res is not None and res is not False) else ""
            except Exception:
                return ""

        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const elems = Array.from(win.document.querySelectorAll('.ace_editor'));
                const targetLower = normalizeTarget(target);
                const targetEl = findEditorElementForTarget(elems, targetLower, win.document);
                if (targetEl) {{
                    if (typeof win.ace !== 'undefined' && win.ace.edit) return win.ace.edit(targetEl).getValue();
                    if (targetEl.env && targetEl.env.editor) return targetEl.env.editor.getValue();
                }}
                if (targetLower) return false;
                elems.sort((a, b) => {{
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                }});
                if (targetLower) return null;
                for (const el of elems) {{
                    if (typeof win.ace !== 'undefined' && win.ace.edit) return win.ace.edit(el).getValue();
                    if (el.env && el.env.editor) return el.env.editor.getValue();
                }}
                return null;
            }}) || "";
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if (res is not None and res is not False) else ""
        except Exception:
            return ""

    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const el = win.document.querySelector('.ace_editor');
                if (el && typeof win.ace !== 'undefined' && win.ace.edit) {{
                    const session = win.ace.edit(el).getSession();
                    return session && session.getMode() ? session.getMode().$id : null;
                }}
                return null;
            }});
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res) if res else None
        except Exception:
            return None

    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None


class TextareaAdapter(EditorAdapter):
    """Adapter for generic textarea and contenteditable."""
    async def detect(self, page: Any) -> bool:
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const elems = Array.from(win.document.querySelectorAll('textarea, [contenteditable="true"]')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                for (const el of elems) {{
                    if (el.offsetParent !== null || el.getBoundingClientRect().height > 40) return true;
                }}
                if (elems.length > 0) return true;
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def snapshot(self, page: Any, target: Optional[str] = None) -> WorkspaceSnapshot:
        content = await self.readback(page, target=target)
        snap = WorkspaceSnapshot(
            editor_kind="textarea",
            content=content,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str, target: Optional[str] = None) -> bool:
        if not target:
            script = f"""(code) => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return false;
                    const tas = Array.from(win.document.querySelectorAll('textarea')).filter(
                        el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                    );
                    tas.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    const el = tas.length > 0 ? tas[0] : null;
                    if (el) {{
                        el.focus();
                        el.value = code;
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    const ces = Array.from(win.document.querySelectorAll('[contenteditable="true"]')).filter(
                        ce => !ce.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                    );
                    ces.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    const ce = ces.length > 0 ? ces[0] : null;
                    if (ce) {{
                        ce.focus();
                        try {{
                            const sel = win.getSelection();
                            const range = win.document.createRange();
                            range.selectNodeContents(ce);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            if (win.document.execCommand('insertText', false, code)) return true;
                        }} catch (e) {{}}
                        ce.innerText = code;
                        ce.dispatchEvent(new Event('input', {{bubbles: true}}));
                        ce.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }}) || false;
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script, code), timeout=10.0)
                return str(res).lower() == "true" or res is True
            except Exception:
                return False

        script = f"""({{code, target}}) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const targetLower = normalizeTarget(target);
                const tas = Array.from(win.document.querySelectorAll('textarea')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                if (tas.length > 0) {{
                    const targetEl = findEditorElementForTarget(tas, targetLower, win.document);
                    const el = targetEl;
                    if (el) {{
                        el.focus();
                        el.value = code;
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                }}
                const ces = Array.from(win.document.querySelectorAll('[contenteditable="true"]')).filter(
                    ce => !ce.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                const targetCe = findEditorElementForTarget(ces, targetLower, win.document);
                const ce = targetCe;
                if (ce) {{
                    ce.focus();
                    try {{
                        const sel = win.getSelection();
                        const range = win.document.createRange();
                        range.selectNodeContents(ce);
                        sel.removeAllRanges();
                        sel.addRange(range);
                        if (win.document.execCommand('insertText', false, code)) return true;
                    }} catch (e) {{}}
                    ce.innerText = code;
                    ce.dispatchEvent(new Event('input', {{bubbles: true}}));
                    ce.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
                return false;
            }}) || false;
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, {"code": code, "target": target}), timeout=10.0)
            return str(res).lower() == "true" or res is True
        except Exception:
            return False

    async def readback(self, page: Any, target: Optional[str] = None) -> str:
        if not target:
            script = f"""() => {{
                {JS_RECURSIVE_SEARCH}
                return searchIframes((win) => {{
                    if (!win.document) return null;
                    const tas = Array.from(win.document.querySelectorAll('textarea')).filter(
                        el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                    );
                    tas.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    const el = tas.length > 0 ? tas[0] : null;
                    if (el) return el.value;
                    const ces = Array.from(win.document.querySelectorAll('[contenteditable="true"]')).filter(
                        ce => !ce.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                    );
                    ces.sort((a, b) => {{
                        const rA = a.getBoundingClientRect();
                        const rB = b.getBoundingClientRect();
                        return (rB.width * rB.height) - (rA.width * rA.height);
                    }});
                    const ce = ces.length > 0 ? ces[0] : null;
                    if (ce) return ce.innerText || ce.textContent || "";
                    return null;
                }}) || "";
            }}"""
            try:
                res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
                return str(res) if (res is not None and res is not False) else ""
            except Exception:
                return ""

        script = f"""(target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return null;
                const targetLower = normalizeTarget(target);
                const tas = Array.from(win.document.querySelectorAll('textarea')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                if (tas.length > 0) {{
                    const targetEl = findEditorElementForTarget(tas, targetLower, win.document);
                    const el = targetEl;
                    if (el) return el.value;
                }}
                const ces = Array.from(win.document.querySelectorAll('[contenteditable="true"]')).filter(
                    ce => !ce.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                const targetCe = findEditorElementForTarget(ces, targetLower, win.document);
                const ce = targetCe;
                if (ce) return ce.innerText || ce.textContent || "";
                return null;
            }}) || "";
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script, target), timeout=10.0)
            return str(res) if (res is not None and res is not False) else ""
        except Exception:
            return ""

    async def get_language(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None

    async def get_model_id(self, page: Any, target: Optional[str] = None) -> Optional[str]:
        return None


async def detect_editor(page: Any) -> Optional[EditorAdapter]:
    """
    Try each adapter in priority order (Monaco first, then CM5, CM6, Ace, Textarea),
    return the first that detects successfully, or None.
    """
    adapters = [
        MonacoAdapter(),
        CodeMirror5Adapter(),
        CodeMirror6Adapter(),
        AceAdapter(),
        TextareaAdapter()
    ]
    for adapter in adapters:
        if await adapter.detect(page):
            return adapter
    return None


async def get_all_editors_summary(page: Any) -> List[Dict[str, Any]]:
    """
    Inspect the page and return metadata for all detected code editors across all iframes.
    """
    if page is None:
        return []
    script = f"""() => {{
        {JS_RECURSIVE_SEARCH}
        {JS_MULTI_EDITOR_HELPERS}
        const results = [];
        searchIframes((win) => {{
            if (!win.document) return null;
            // 1. Monaco models
            if (typeof win.monaco !== 'undefined' && win.monaco.editor) {{
                const models = win.monaco.editor.getModels ? win.monaco.editor.getModels() : [];
                const valid = models.filter(m => !m.uri || !m.uri.path.endsWith('.d.ts'));
                if (valid.length > 0) {{
                    valid.forEach((m, idx) => {{
                        const lang = (m.getLanguageId ? m.getLanguageId() : '').toLowerCase();
                        const path = m.uri ? (m.uri.path || m.uri.toString()) : '';
                        const targetKey = (lang === 'css' || path.endsWith('.css')) ? 'css' :
                                          (lang === 'html' || path.endsWith('.html') || path.endsWith('.htm')) ? 'html' :
                                          (lang === 'javascript' || path.endsWith('.js')) ? 'javascript' :
                                          (lang || 'editor_' + idx);
                        results.push({{
                            'index': results.length,
                            'kind': 'monaco',
                            'language': lang || (path.endsWith('.css') ? 'css' : path.endsWith('.html') ? 'html' : 'unknown'),
                            'label': path || (lang ? lang.toUpperCase() : 'Editor ' + (idx + 1)),
                            'target_key': path || targetKey,
                            'content': m.getValue(),
                            'is_active': true
                        }});
                    }});
                    return true;
                }}
            }}
            const monacoNodes = Array.from(win.document.querySelectorAll('.monaco-editor'));
            if (monacoNodes.length) {{
                monacoNodes.forEach((el, idx) => results.push({{
                    index: results.length, kind: 'monaco', language: editorLanguage(el, win.document),
                    label: editorEvidence(el, win.document).filter(Boolean).join(' / ') || 'Editor ' + (idx + 1),
                    target_key: 'monaco:editor_' + idx,
                    content: Array.from(el.querySelectorAll('.view-line')).map(line => line.textContent).join('\\n'),
                    is_active: !!el.getClientRects().length
                }}));
                return true;
            }}
            // 2. CodeMirror 5
            const cm5s = Array.from(win.document.querySelectorAll('.CodeMirror'));
            if (cm5s.length > 0) {{
                cm5s.forEach((el, idx) => {{
                    if (el.CodeMirror) {{
                        const mode = el.CodeMirror.getOption('mode');
                        const modeStr = typeof mode === 'string' ? mode : (mode && mode.name ? mode.name : '');
                        const lang = editorLanguage(el, win.document);
                        const targetKey = 'editor_' + idx;
                        results.push({{
                            'index': results.length,
                            'kind': 'codemirror5',
                            'language': lang,
                            'label': targetKey.toUpperCase() + ' Editor',
                            'target_key': 'codemirror5:' + targetKey,
                            'content': el.CodeMirror.getValue(),
                            'is_active': el.offsetParent !== null
                        }});
                    }}
                }});
                return true;
            }}
            // 3. CodeMirror 6
            const cm6s = Array.from(win.document.querySelectorAll('.cm-editor'));
            if (cm6s.length > 0) {{
                cm6s.forEach((el, idx) => {{
                    const targetKey = 'editor_' + idx;
                    const lang = editorLanguage(el, win.document);
                    const view = el.cmView && (el.cmView.view || el.cmView);
                    const content = view && view.state && view.state.doc ? view.state.doc.toString() : (el.textContent || '');
                    results.push({{
                        'index': results.length,
                        'kind': 'codemirror6',
                        'language': lang,
                        'label': targetKey.toUpperCase() + ' Editor',
                        'target_key': 'codemirror6:' + targetKey,
                        'content': content,
                        'is_active': el.offsetParent !== null
                    }});
                }});
                return true;
            }}
            // 4. Ace
            const aces = Array.from(win.document.querySelectorAll('.ace_editor'));
            if (aces.length > 0) {{
                aces.forEach((el, idx) => {{
                    const win2 = win.document.defaultView || win;
                    let ed = null;
                    if (win2.ace && win2.ace.edit) ed = win2.ace.edit(el);
                    else if (el.env && el.env.editor) ed = el.env.editor;
                    if (ed) {{
                        const session = ed.getSession ? ed.getSession() : null;
                        const mode = session && session.getMode ? (session.getMode().$id || '') : '';
                        const lang = ['html','css','javascript'].find(value => matchesTargetKeywords(mode, value)) || editorLanguage(el, win.document);
                        const targetKey = 'editor_' + idx;
                        results.push({{
                            'index': results.length,
                            'kind': 'ace',
                            'language': lang,
                            'label': targetKey.toUpperCase() + ' Editor',
                            'target_key': 'ace:' + targetKey,
                            'content': ed.getValue ? ed.getValue() : '',
                            'is_active': el.offsetParent !== null
                        }});
                    }}
                }});
                return true;
            }}
            const textareas = Array.from(win.document.querySelectorAll('textarea')).filter(el =>
                !el.closest('#__omnitasker_panel, .monaco-editor, .CodeMirror, .cm-editor, .ace_editor'));
            textareas.forEach((el, idx) => results.push({{
                index: results.length, kind: 'textarea', language: editorLanguage(el, win.document),
                label: el.getAttribute('aria-label') || el.name || el.id || 'Editor ' + (idx + 1),
                target_key: 'textarea:editor_' + idx, content: el.value, is_active: !!el.getClientRects().length
            }}));
            return null;
        }});
        return results;
    }}"""
    try:
        res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
        if isinstance(res, str):
            res = json.loads(res)
        return res if isinstance(res, list) else []
    except Exception:
        return []


async def _safe_write_single(
    page: Any,
    code: str,
    adapter: Optional[EditorAdapter] = None,
    target: Optional[str] = None,
) -> Tuple[bool, WorkspaceSnapshot, str]:
    if target and ':' in target:
        kind, _, local_key = target.partition(':')
        kinds = {"monaco": MonacoAdapter, "codemirror5": CodeMirror5Adapter,
                 "codemirror6": CodeMirror6Adapter, "ace": AceAdapter, "textarea": TextareaAdapter}
        if kind in kinds:
            adapter, target = kinds[kind](), local_key
    adapters_to_try = []
    if adapter is not None:
        adapters_to_try.append(adapter)
    else:
        primary = await detect_editor(page)
        if primary is not None:
            adapters_to_try.append(primary)
        all_adapters = [
            MonacoAdapter(),
            CodeMirror5Adapter(),
            CodeMirror6Adapter(),
            AceAdapter(),
            TextareaAdapter(),
        ]
        for a in all_adapters:
            if not any(type(a) is type(existing) for existing in adapters_to_try):
                adapters_to_try.append(a)

    if not adapters_to_try:
        return False, WorkspaceSnapshot(content=""), ""

    def normalize(s: str) -> str:
        return (s or "").replace("\r\n", "\n").strip()

    norm_code = normalize(code)
    last_snap = WorkspaceSnapshot(content="")
    last_readback = ""

    for ad in adapters_to_try:
        try:
            detected = await ad.detect(page)
            if not detected and len(adapters_to_try) > 1 and ad is not adapters_to_try[0]:
                continue

            last_snap = await ad.snapshot(page, target=target)
            if normalize(last_snap.content) == norm_code:
                readback_content = await ad.readback(page, target=target)
                if normalize(readback_content) == norm_code:
                    return True, last_snap, readback_content
            success = await ad.write(page, code, target=target)
            readback_content = await ad.readback(page, target=target)
            last_readback = readback_content
            norm_readback = normalize(readback_content)

            if success:
                if norm_readback == norm_code:
                    return True, last_snap, readback_content
        except Exception:
            logger.warning("Editor write failed (%s, target=%r)", type(ad).__name__, target, exc_info=True)
            continue

    return False, last_snap, last_readback


async def safe_write(
    page: Any,
    code: str,
    adapter: Optional[EditorAdapter] = None,
    target: Optional[str] = None,
) -> Tuple[bool, WorkspaceSnapshot, str]:
    """
    Perform a safe write cycle to the editor with snapshot-before-write, write, and normalized readback verification.
    Supports multi-editor targeting (HTML, CSS, JS) and automatic multi-code decomposition.
    """
    editors = await get_all_editors_summary(page)
    if target is not None:
        parts = split_multi_editor_code(code)
        if len(editors) > 1 and len(parts) > 1:
            available = {e.get("language") for e in editors}
            if set(parts) <= available:
                # A combined response must use each observed destination, never one forced pane.
                target = None
            elif "```" in code:
                return False, WorkspaceSnapshot(content=""), "Multiple source files supplied for one editor; write each file separately."
        if target is not None:
            return await _safe_write_single(page, code, adapter=adapter, target=target)

    # Only distribute source across files that actually exist in this workspace.
    # A single HTML file may legitimately contain inline styles and scripts.
    if len(editors) > 1:
        parts = split_multi_editor_code(code)
        destinations = {}
        for language in parts:
            matches = [e for e in editors if e.get("language") == language]
            if len(matches) != 1:
                return False, WorkspaceSnapshot(content=""), "Ambiguous editor destination; read the editors and provide target_editor."
            destinations[language] = matches[0].get("target_key") or language
        if not parts:
            return False, WorkspaceSnapshot(content=""), "Multiple editors found; provide target_editor."
        first_snapshot = None
        readbacks = []
        for language, source in parts.items():
            ok, snap, readback = await _safe_write_single(
                page, source, adapter=adapter, target=destinations[language])
            if first_snapshot is None:
                first_snapshot = snap
            if not ok:
                return False, first_snapshot, readback
            readbacks.append(f"```{language}\n{readback}\n```")
        return True, first_snapshot, "\n\n".join(readbacks)

    return await _safe_write_single(page, code, adapter=adapter)
