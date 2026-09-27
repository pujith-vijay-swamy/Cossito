"""
monaco_diff_injector.py — Resilient line-by-line diff replacement inside virtualized Monaco & CodeMirror editors.
"""

from typing import Any, Optional


async def monaco_diff_injector(page: Any, code: str, editor_selector: Optional[str] = None) -> str:
    """
    Performs intelligent diff injection into Monaco/VSCode and CodeMirror editors preserving boilerplate templates.
    
    Parameters:
        page: Active Playwright / browser page session.
        code: Complete Python, Java, JS, or C++ source code to inject.
        editor_selector: Optional CSS selector to scope the target editor container.
    """
    result = await page.evaluate("""(data) => {
        const code = data.code;
        const root = data.selector ? document.querySelector(data.selector) : document;
        if (!root) return "Error: Target editor container not found";

        // Monaco Editor
        const win = root.defaultView || window;
        if (win.monaco && win.monaco.editor) {
            const models = win.monaco.editor.getModels();
            if (models && models.length > 0) {
                const model = models[0];
                const existing = model.getValue();
                if (existing === code) return "Monaco code already identical";

                try {
                    model.setValue(code);
                    if (model.getValue() === code) return "Monaco setValue succeeded";
                } catch(e) {}

                const existingLines = existing.split('\\n');
                const newLines = code.split('\\n');
                const clean = s => s ? s.trim().replace(/\\s+/g, '') : '';

                let prefix = 0;
                while (prefix < existingLines.length && prefix < newLines.length && clean(existingLines[prefix]) === clean(newLines[prefix])) {
                    prefix++;
                }

                let suffix = 0;
                while (suffix < (existingLines.length - prefix) && suffix < (newLines.length - prefix) && clean(existingLines[existingLines.length - 1 - suffix]) === clean(newLines[newLines.length - 1 - suffix])) {
                    suffix++;
                }

                const startLine = prefix + 1;
                const endLine = existingLines.length - suffix;
                const replacementText = newLines.slice(prefix, newLines.length - suffix).join('\\n');

                const editors = win.monaco.editor.getEditors();
                if (editors && editors.length > 0) {
                    editors[0].executeEdits("monaco-diff-skill", [{
                        range: new win.monaco.Range(startLine, 1, Math.max(startLine, endLine), 999999),
                        text: replacementText,
                        forceMoveMarkers: true
                    }]);
                    return "Monaco diff update succeeded on lines " + startLine + "-" + endLine;
                }
            }
        }

        // CodeMirror
        const cm = root.querySelector('.CodeMirror');
        if (cm && cm.CodeMirror) {
            cm.CodeMirror.setValue(code);
            return "CodeMirror setValue succeeded";
        }

        // Textarea Fallback
        const ta = root.querySelector('textarea');
        if (ta) {
            ta.focus();
            ta.value = code;
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
            return "Textarea value updated";
        }

        return "No supported editor matched for injection";
    }""", {"code": code, "selector": editor_selector})

    return str(result)
