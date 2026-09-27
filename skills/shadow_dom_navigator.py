"""
shadow_dom_navigator.py — Traverses Shadow DOM roots and nested same-origin IFrames to find and interact with hidden elements.
"""

from typing import Any, Dict, Optional


async def shadow_dom_navigator(page: Any, selector: str, action: str = "click", text_to_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Finds and interacts with elements deeply nested inside open/closed Shadow Roots or IFrames.
    
    Parameters:
        page: Active Playwright / browser page session.
        selector: CSS selector or text pattern to locate inside shadow roots.
        action: Interaction type: 'click', 'type', or 'getText'.
        text_to_type: Optional text if action is 'type'.
    """
    result = await page.evaluate("""(params) => {
        function queryDeep(root, sel) {
            if (!root) return null;
            let el = root.querySelector(sel);
            if (el) return el;

            // Search shadow roots
            const all = root.querySelectorAll('*');
            for (const item of all) {
                if (item.shadowRoot) {
                    const found = queryDeep(item.shadowRoot, sel);
                    if (found) return found;
                }
            }

            // Search same-origin iframes
            const iframes = root.querySelectorAll('iframe');
            for (const iframe of iframes) {
                try {
                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    if (doc) {
                        const found = queryDeep(doc, sel);
                        if (found) return found;
                    }
                } catch(e) {}
            }
            return null;
        }

        const target = queryDeep(document, params.selector);
        if (!target) {
            return { success: false, message: "Element not found in Shadow DOM or IFrames: " + params.selector };
        }

        if (params.action === 'click') {
            target.scrollIntoView({ behavior: 'instant', block: 'center' });
            target.click();
            return { success: true, message: "Clicked element in Shadow DOM" };
        } else if (params.action === 'type') {
            target.focus();
            if ('value' in target) {
                target.value = params.text || '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
            } else {
                target.textContent = params.text || '';
            }
            return { success: true, message: "Typed text into element in Shadow DOM" };
        } else if (params.action === 'getText') {
            return { success: true, text: target.innerText || target.textContent || '' };
        }

        return { success: true, message: "Located target element" };
    }""", {"selector": selector, "action": action, "text": text_to_type})

    return result
