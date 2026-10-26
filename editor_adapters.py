"""
Module for interacting with various code editors in browser automation.
Implements safe editor interaction with snapshot-before-write, readback verification,
and multi-editor support.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, Any, Tuple

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

class EditorAdapter(ABC):
    """Abstract base class for editor adapters."""

    @abstractmethod
    async def detect(self, page: Any) -> bool:
        """Detect if this editor type is present on the page."""
        pass

    @abstractmethod
    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        """Capture the current state of the editor."""
        pass

    @abstractmethod
    async def write(self, page: Any, code: str) -> bool:
        """Write code to the editor."""
        pass

    @abstractmethod
    async def readback(self, page: Any) -> str:
        """Read the current content of the editor."""
        pass

    @abstractmethod
    async def get_language(self, page: Any) -> Optional[str]:
        """Detect the current language of the editor."""
        pass

    @abstractmethod
    async def get_model_id(self, page: Any) -> Optional[str]:
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

    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        content = await self.readback(page)
        lang = await self.get_language(page)
        model_id = await self.get_model_id(page)
        snap = WorkspaceSnapshot(
            editor_kind="monaco",
            content=str(content or ""),
            language=str(lang) if isinstance(lang, str) else None,
            model_id=str(model_id) if isinstance(model_id, str) else None,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str) -> bool:
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

    async def readback(self, page: Any) -> str:
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

    async def get_language(self, page: Any) -> Optional[str]:
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

    async def get_model_id(self, page: Any) -> Optional[str]:
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

    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        content = await self.readback(page)
        lang = await self.get_language(page)
        snap = WorkspaceSnapshot(
            editor_kind="codemirror5",
            content=content,
            language=lang,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str) -> bool:
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

    async def readback(self, page: Any) -> str:
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

    async def get_language(self, page: Any) -> Optional[str]:
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

    async def get_model_id(self, page: Any) -> Optional[str]:
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

    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        content = await self.readback(page)
        snap = WorkspaceSnapshot(
            editor_kind="codemirror6",
            content=content,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str) -> bool:
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

    async def readback(self, page: Any) -> str:
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

    async def get_language(self, page: Any) -> Optional[str]:
        return None

    async def get_model_id(self, page: Any) -> Optional[str]:
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

    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        content = await self.readback(page)
        lang = await self.get_language(page)
        snap = WorkspaceSnapshot(
            editor_kind="ace",
            content=content,
            language=lang,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str) -> bool:
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

    async def readback(self, page: Any) -> str:
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

    async def get_language(self, page: Any) -> Optional[str]:
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

    async def get_model_id(self, page: Any) -> Optional[str]:
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

    async def snapshot(self, page: Any) -> WorkspaceSnapshot:
        content = await self.readback(page)
        snap = WorkspaceSnapshot(
            editor_kind="textarea",
            content=content,
        )
        snap.compute_hash()
        return snap

    async def write(self, page: Any, code: str) -> bool:
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

    async def readback(self, page: Any) -> str:
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

    async def get_language(self, page: Any) -> Optional[str]:
        return None

    async def get_model_id(self, page: Any) -> Optional[str]:
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


async def safe_write(page: Any, code: str, adapter: Optional[EditorAdapter] = None) -> Tuple[bool, WorkspaceSnapshot, str]:
    """
    Perform a safe write cycle to the editor with snapshot-before-write, write, and normalized readback verification.
    If the primary adapter write or readback verification fails, automatically fails over to other detected adapters.
    """
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

            last_snap = await ad.snapshot(page)
            # Re-submitting unchanged code must not fire editor change events
            # which can clear a platform's already-visible validation result.
            if normalize(last_snap.content) == norm_code:
                readback_content = await ad.readback(page)
                if normalize(readback_content) == norm_code:
                    return True, last_snap, readback_content
            success = await ad.write(page, code)
            readback_content = await ad.readback(page)
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
