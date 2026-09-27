"""
handle_blocking_modals.py — Autonomous helper to dismiss blocking modals, overlays, and cookie banners.
"""

from typing import Any, Dict


async def handle_blocking_modals(page: Any, max_dismissals: int = 5) -> Dict[str, Any]:
    """
    Detects and dismisses blocking dialogs, cookie consent banners, backdrops, and promotional popups.
    
    Parameters:
        page: Active Playwright / browser page session.
        max_dismissals: Maximum number of dismissable overlays to close in one pass.
    """
    result = await page.evaluate("""(maxCount) => {
        let dismissed = [];
        const closeSelectors = [
            'button[aria-label*="close" i]', 'button[aria-label*="dismiss" i]',
            'button[class*="close" i]', '.modal-close', '.popup-close',
            'button:has-text("Accept All")', 'button:has-text("I Agree")',
            'button:has-text("Got it")', 'button:has-text("Dismiss")',
            'button:has-text("No thanks")', 'button:has-text("Maybe later")',
            '[data-dismiss="modal"]', '.close-btn'
        ];

        for (let i = 0; i < maxCount; i++) {
            let found = false;
            for (const sel of closeSelectors) {
                try {
                    const btns = Array.from(document.querySelectorAll(sel));
                    for (const btn of btns) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && !btn.disabled) {
                            btn.click();
                            dismissed.push(sel);
                            found = true;
                            break;
                        }
                    }
                } catch(e) {}
                if (found) break;
            }
            if (!found) break;
        }

        // Also remove any full-page zero-pointer-event blocking backdrops if present
        const backdrops = Array.from(document.querySelectorAll('.modal-backdrop, .overlay-backdrop, [class*="backdrop"]'));
        for (const bd of backdrops) {
            bd.style.display = 'none';
            dismissed.push('removed-backdrop');
        }

        return { count: dismissed.length, dismissed: dismissed };
    }""", max_dismissals)

    return result
