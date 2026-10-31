"""
Module for interacting with various code editors in browser automation.
Implements safe editor interaction with snapshot-before-write, readback verification,
and multi-editor support.
"""
from __future__ import annotations

import asyncio
import logging
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

JS_MULTI_EDITOR_HELPERS = """
function normalizeTarget(t) {
    return (t || '').toLowerCase().trim();
}

function matchesTargetKeywords(text, target) {
    if (!text || !target) return false;
    const lower = text.toLowerCase();
    const t = normalizeTarget(target);
    if (t === 'css') {
        return lower === 'css' || lower.includes('css') || lower.includes('style') || lower.includes('stylesheet');
    }
    if (t === 'html') {
        return lower === 'html' || lower.includes('html') || lower.includes('index.html');
    }
    if (t === 'js' || t === 'javascript') {
        return lower === 'js' || lower === 'javascript' || lower.includes('script.js');
    }
    return lower === t || lower.includes(t);
}

function clickMatchingTab(doc, target) {
    if (!doc || !target) return false;
    const t = normalizeTarget(target);
    const keywords = (t === 'css') ? [/^css$/i, /style\\.css/i, /styles\\.css/i, /stylesheet/i] :
                     (t === 'html') ? [/^html$/i, /index\\.html/i] :
                     (t === 'js' || t === 'javascript') ? [/^js$/i, /^javascript$/i, /script\\.js/i] :
                     [new RegExp('^' + t + '$', 'i')];

    const candidates = Array.from(doc.querySelectorAll('button, [role="tab"], .tab, .nav-link, a, span, div')).filter(el => {
        const txt = (el.textContent || '').trim();
        return keywords.some(rx => rx.test(txt)) && el.getClientRects().length > 0;
    });
    for (const tab of candidates) {
        const isSelected = tab.getAttribute('aria-selected') === 'true' ||
                           tab.classList.contains('active') ||
                           tab.classList.contains('selected');
        if (!isSelected) {
            try {
                tab.click();
                return true;
            } catch(e) {}
        }
    }
    return false;
}

function findEditorElementForTarget(elements, target, doc) {
    if (!elements || elements.length === 0) return null;
    if (elements.length === 1 && !target) return elements[0];
    const t = normalizeTarget(target);
    if (!t) {
        return elements.find(el => el.offsetParent !== null) || elements[0];
    }

    // 1. Check element attributes
    for (const el of elements) {
        const id = el.id || '';
        const cls = el.className || '';
        const dataLang = el.getAttribute('data-lang') || el.getAttribute('data-language') || el.getAttribute('data-mode') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const name = el.getAttribute('name') || '';
        if (matchesTargetKeywords(id, t) || matchesTargetKeywords(cls, t) || matchesTargetKeywords(dataLang, t) || matchesTargetKeywords(ariaLabel, t) || matchesTargetKeywords(name, t)) {
            return el;
        }
    }

    // 2. Check surrounding labels, headers, or tab panels
    for (const el of elements) {
        let curr = el;
        for (let depth = 0; depth < 5 && curr && curr !== doc.body; depth++) {
            const heading = curr.querySelector('h1, h2, h3, h4, h5, h6, .tab, .tab-pane, .header, .title, .filename, label, [role="tab"], .nav-link, span');
            if (heading && matchesTargetKeywords(heading.textContent, t)) {
                return el;
            }
            const prev = curr.previousElementSibling;
            if (prev && matchesTargetKeywords(prev.textContent, t)) {
                return el;
            }
            const pData = curr.getAttribute('data-tab') || curr.getAttribute('data-lang') || curr.id || curr.className || '';
            if (matchesTargetKeywords(pData, t)) {
                return el;
            }
            curr = curr.parentElement;
        }
    }

    // 3. Check editor internal session / mode
    for (const el of elements) {
        try {
            const win = doc.defaultView || window;
            if (win.ace && win.ace.edit) {
                const ed = win.ace.edit(el);
                const mode = ed.getSession().getMode().$id || '';
                if (matchesTargetKeywords(mode, t)) return el;
            }
            if (el.env && el.env.editor) {
                const mode = el.env.editor.getSession().getMode().$id || '';
                if (matchesTargetKeywords(mode, t)) return el;
            }
        } catch(e) {}
        try {
            if (el.CodeMirror) {
                const mode = el.CodeMirror.getOption('mode');
                const modeStr = typeof mode === 'string' ? mode : (mode && mode.name ? mode.name : '');
                if (matchesTargetKeywords(modeStr, t)) return el;
            }
        } catch(e) {}
    }

    // 4. Layout order fallback for standard multi-editor setups
    if (elements.length >= 2) {
        if (t === 'html') return elements[0];
        if (t === 'css') return elements[1];
        if (t === 'js' || t === 'javascript') return elements[2] || elements[elements.length - 1];
    }

    return elements.find(el => el.offsetParent !== null) || elements[0];
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

    if html_blocks and css_blocks:
        result["html"] = "\n\n".join(b.strip() for b in html_blocks)
        result["css"] = "\n\n".join(b.strip() for b in css_blocks)
        if js_blocks:
            result["javascript"] = "\n\n".join(b.strip() for b in js_blocks)
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

        script = f"""(code, target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                const targetLower = normalizeTarget(target);
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels ? win.monaco.editor.getModels() : [];
                    const eds = win.monaco.editor.getEditors ? win.monaco.editor.getEditors() : [];
                    const validModels = models.filter(m => !m.uri || !m.uri.path.endsWith('.d.ts'));

                    if (targetLower) {{
                        let matchedModel = validModels.find(m => {{
                            const lang = (m.getLanguageId ? m.getLanguageId() : '').toLowerCase();
                            const path = (m.uri ? (m.uri.path || m.uri.toString()) : '').toLowerCase();
                            return matchesTargetKeywords(lang, targetLower) || matchesTargetKeywords(path, targetLower);
                        }});
                        if (matchedModel) {{
                            matchedModel.setValue(code);
                            for (const ed of eds) {{
                                try {{
                                    if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {{
                                        ed.setModel(matchedModel);
                                        break;
                                    }}
                                }} catch(e) {{}}
                            }}
                            if (win.document) clickMatchingTab(win.document, targetLower);
                            return true;
                        }}
                        for (const ed of eds) {{
                            try {{
                                const m = ed.getModel();
                                const lang = m && m.getLanguageId ? m.getLanguageId().toLowerCase() : '';
                                const path = m && m.uri ? (m.uri.path || m.uri.toString()).toLowerCase() : '';
                                if (matchesTargetKeywords(lang, targetLower) || matchesTargetKeywords(path, targetLower)) {{
                                    ed.setValue(code);
                                    return true;
                                }}
                            }} catch(e) {{}}
                        }}
                    }}

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
            res = await asyncio.wait_for(page.evaluate(script, code, target), timeout=10.0)
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
                        const matched = validModels.find(m => {{
                            const lang = (m.getLanguageId ? m.getLanguageId() : '').toLowerCase();
                            const path = (m.uri ? (m.uri.path || m.uri.toString()) : '').toLowerCase();
                            return matchesTargetKeywords(lang, targetLower) || matchesTargetKeywords(path, targetLower);
                        }});
                        if (matched) return matched.getValue();
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
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels();
                    if (models.length > 0 && models[0].getLanguageId) return models[0].getLanguageId();
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
        script = f"""() => {{
            {JS_RECURSIVE_SEARCH}
            return searchIframes((win) => {{
                if (typeof win.monaco !== 'undefined' && typeof win.monaco.editor !== 'undefined') {{
                    const models = win.monaco.editor.getModels();
                    if (models.length > 0 && models[0].uri) return models[0].uri.toString();
                }}
                return null;
            }});
        }}"""
        try:
            res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
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

        script = f"""(code, target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const elems = Array.from(win.document.querySelectorAll('.CodeMirror'));
                if (elems.length === 0) return false;
                const targetLower = normalizeTarget(target);
                if (targetLower && win.document) {{
                    clickMatchingTab(win.document, targetLower);
                }}
                const targetEl = findEditorElementForTarget(elems, targetLower, win.document);
                if (targetEl && targetEl.CodeMirror) {{
                    targetEl.CodeMirror.setValue(code);
                    if (targetEl.CodeMirror.save) targetEl.CodeMirror.save();
                    return true;
                }}
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
            res = await asyncio.wait_for(page.evaluate(script, code, target), timeout=10.0)
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

        script = f"""(code, target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const cmEditors = Array.from(win.document.querySelectorAll('.cm-editor'));
                const targetLower = normalizeTarget(target);
                if (targetLower && win.document) {{
                    clickMatchingTab(win.document, targetLower);
                }}
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
            res = await asyncio.wait_for(page.evaluate(script, code, target), timeout=10.0)
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

        script = f"""(code, target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const elems = Array.from(win.document.querySelectorAll('.ace_editor'));
                const targetLower = normalizeTarget(target);
                if (targetLower && win.document) {{
                    clickMatchingTab(win.document, targetLower);
                }}
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
            res = await asyncio.wait_for(page.evaluate(script, code, target), timeout=10.0)
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

        script = f"""(code, target) => {{
            {JS_RECURSIVE_SEARCH}
            {JS_MULTI_EDITOR_HELPERS}
            return searchIframes((win) => {{
                if (!win.document) return false;
                const targetLower = normalizeTarget(target);
                if (targetLower && win.document) {{
                    clickMatchingTab(win.document, targetLower);
                }}
                const tas = Array.from(win.document.querySelectorAll('textarea')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                if (tas.length > 0) {{
                    const targetEl = findEditorElementForTarget(tas, targetLower, win.document);
                    const el = targetEl || tas[0];
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
                const ce = targetCe || (ces.length > 0 ? ces[0] : null);
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
            res = await asyncio.wait_for(page.evaluate(script, code, target), timeout=10.0)
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
                    const el = targetEl || tas[0];
                    if (el) return el.value;
                }}
                const ces = Array.from(win.document.querySelectorAll('[contenteditable="true"]')).filter(
                    ce => !ce.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                const targetCe = findEditorElementForTarget(ces, targetLower, win.document);
                const ce = targetCe || (ces.length > 0 ? ces[0] : null);
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
                            'target_key': targetKey,
                            'content': m.getValue(),
                            'is_active': true
                        }});
                    }});
                    return true;
                }}
            }}
            // 2. CodeMirror 5
            const cm5s = Array.from(win.document.querySelectorAll('.CodeMirror'));
            if (cm5s.length > 0) {{
                cm5s.forEach((el, idx) => {{
                    if (el.CodeMirror) {{
                        const mode = el.CodeMirror.getOption('mode');
                        const modeStr = typeof mode === 'string' ? mode : (mode && mode.name ? mode.name : '');
                        const lang = modeStr.includes('css') ? 'css' : (modeStr.includes('html') || modeStr.includes('xml')) ? 'html' : 'unknown';
                        const targetKey = lang !== 'unknown' ? lang : (idx === 0 ? 'html' : idx === 1 ? 'css' : 'editor_' + idx);
                        results.push({{
                            'index': results.length,
                            'kind': 'codemirror5',
                            'language': lang !== 'unknown' ? lang : targetKey,
                            'label': targetKey.toUpperCase() + ' Editor',
                            'target_key': targetKey,
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
                    const targetKey = idx === 0 ? 'html' : idx === 1 ? 'css' : 'editor_' + idx;
                    const view = el.cmView && (el.cmView.view || el.cmView);
                    const content = view && view.state && view.state.doc ? view.state.doc.toString() : (el.textContent || '');
                    results.push({{
                        'index': results.length,
                        'kind': 'codemirror6',
                        'language': targetKey,
                        'label': targetKey.toUpperCase() + ' Editor',
                        'target_key': targetKey,
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
                        const lang = mode.includes('css') ? 'css' : mode.includes('html') ? 'html' : 'unknown';
                        const targetKey = lang !== 'unknown' ? lang : (idx === 0 ? 'html' : idx === 1 ? 'css' : 'editor_' + idx);
                        results.push({{
                            'index': results.length,
                            'kind': 'ace',
                            'language': lang !== 'unknown' ? lang : targetKey,
                            'label': targetKey.toUpperCase() + ' Editor',
                            'target_key': targetKey,
                            'content': ed.getValue ? ed.getValue() : '',
                            'is_active': el.offsetParent !== null
                        }});
                    }}
                }});
                return true;
            }}
            return null;
        }});
        return results;
    }}"""
    try:
        res = await asyncio.wait_for(page.evaluate(script), timeout=10.0)
        return res if isinstance(res, list) else []
    except Exception:
        return []


async def _safe_write_single(
    page: Any,
    code: str,
    adapter: Optional[EditorAdapter] = None,
    target: Optional[str] = None,
) -> Tuple[bool, WorkspaceSnapshot, str]:
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
                if norm_code and norm_code in norm_readback:
                    return True, last_snap, readback_content
                if norm_code and norm_readback and (norm_readback in norm_code) and len(norm_readback) >= 0.8 * len(norm_code):
                    return True, last_snap, readback_content
        except Exception:
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
    # 1. Multi-code split: if code contains both HTML and CSS, and target is not forced
    if target is None:
        parts = split_multi_editor_code(code)
        if "html" in parts and "css" in parts:
            h_ok, h_snap, h_read = await _safe_write_single(page, parts["html"], adapter=adapter, target="html")
            c_ok, c_snap, c_read = await _safe_write_single(page, parts["css"], adapter=adapter, target="css")
            if h_ok and c_ok:
                return True, h_snap, f"/* Verified: HTML ({len(h_read)} chars) and CSS ({len(c_read)} chars) written */"
            if h_ok or c_ok:
                return True, h_snap if h_ok else c_snap, h_read if h_ok else c_read

        # 2. Single language detection
        detected_lang = classify_code_language(code)
        if detected_lang in ("html", "css", "javascript"):
            target = detected_lang

    return await _safe_write_single(page, code, adapter=adapter, target=target)
