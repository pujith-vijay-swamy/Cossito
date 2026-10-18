"""
dom_extraction.py — DOM extraction and code injection utilities for OmniTasker.

Contains JS evaluation functions extracted from AgentOrchestrator
for problem statement extraction, editor code injection, and reference
image extraction.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional


def is_section_header_text(txt: str) -> bool:
    """Identify if a string represents a section header/category rather than a leaf problem."""
    if not txt:
        return False
    t = txt.strip()
    patterns = [
        r'^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)?(practice\s*problems?|practice\s*questions?|problem\s*set|exercises?|assignments?|topics?|modules?|chapters?|sections?|challenges?|contests?|assessments?|homework|tutorials?|labs?|units?|lessons?|milestones?|sessions?)(?:[\s:\-–—\d\(\)\[\]#]+.*)?$',
        r'^(chapter|module|section|week|day|part|unit|lesson|milestone|session|lab|topic|level)\s*[\d#]+(?:[\s:\-–—]+.*)?$',
        r'^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)(problems?|questions?|exercises?|challenges?|tasks?)$',
        r'^(all\s*problems?|problem\s*list|question\s*list|all\s*questions|practice|problems|questions|challenges)$',
        r'(\d+\s*problems?|\d+\s*questions?|\d+\s*tasks?|\d+\s*items?|\d+/\d+\s*completed)',
        r'^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)?(loops?|conditionals?|functions?|methods?|classes?|oop|arrays?|strings?|linked\s*lists?|stacks?(?:\s*and\s*queues?)?|queues?|trees?(?:\s*and\s*graphs?)?|graphs?|dynamic\s*programming|dp|recursion(?:\s*and\s*backtracking)?|backtracking|binary\s*search|greedy(?:\s*algorithms?)?|bit\s*manipulation|sorting(?:\s*and\s*searching)?|math(?:\s*and\s*logic)?|two\s*pointers|sliding\s*window|hash\s*tables?|heaps?|tries?)(?:[\s:\-–—\(\)\[\]\d]+.*)?$',
    ]
    return any(bool(re.search(p, t, re.IGNORECASE)) for p in patterns)


_is_section_header_text = is_section_header_text


def parse_eval_result(val: Any) -> Any:
    """Safely parse the return value of browser-use page.evaluate (which stringifies JSON/booleans)."""
    if val is None or isinstance(val, (dict, list, bool, int, float)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        if s.lower() in ("null", "undefined"):
            return None
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                pass
    return val


def has_problem_statement_markers(text: str) -> bool:
    """Return True if text contains genuine coding problem specification sections."""
    if not text or not isinstance(text, str):
        return False
    t_lower = text.lower()
    strong_markers = [
        r'\binput\s+format\b',
        r'\boutput\s+format\b',
        r'\bexample\s+input\b',
        r'\bsample\s+input\b',
        r'\bexample\s+output\b',
        r'\bsample\s+output\b',
        r'\bprohibited\s+keywords?\b',
        r'\bconstraints?\s*[:\n\d]',
        r'\bexplanation\s*\d*\b',
    ]
    strong_hits = sum(1 for p in strong_markers if re.search(p, t_lower))
    if strong_hits >= 2:
        return True
    general_markers = [
        r'\bconstraints?\b',
        r'\binput\s*:',
        r'\boutput\s*:',
        r'\bexample\s*1?\s*:',
        r'\bgiven\s+(?:a|an|two|the|n)\b',
        r'\breturn\s+(?:the|true|false|an?|indices)\b',
        r'\bwrite\s+a\s+(?:program|function|method|query)\b',
        r'\brefer\s+to\s+the\s+(?:below\s+)?image\b',
        r'\bdesign\s+a\b',
    ]
    general_hits = sum(1 for p in general_markers if re.search(p, t_lower))
    return (strong_hits + general_hits) >= 2


def clean_extracted_problem_text(text: str) -> str:
    """Strip site navbar, chapter sidebar, and trailing code editor/runner noise from extracted text.

    If the text is purely navigation/overview without a problem statement, returns "".
    """
    if not text or not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if len(cleaned) < 15:
        return ""

    # 1. Strip leading navbar/sidebar/tab bar if present before the problem statement
    # Pattern A: "... Description Submissions <Problem Title> [DIFFICULTY] Description ..."
    m_tabs = re.search(
        r'\bDescription\s+Submissions\s+(?:Editorial\s+|Solutions\s+|Hints\s+|Discuss(?:ions?)?\s+)*([A-Z0-9][\s\S]{20,})$',
        cleaned,
    )
    if m_tabs:
        cleaned = m_tabs.group(1).strip()
    else:
        # Pattern B: "... Next Chapter: <Title> ... <Problem Statement>"
        m_next = re.search(
            r'\bNext\s+Chapter\s*:\s*[^\n]+?(?:\.\.\.|\n)\s*([\s\S]{30,})$',
            cleaned,
            re.IGNORECASE,
        )
        if m_next and has_problem_statement_markers(m_next.group(1)):
            cleaned = m_next.group(1).strip()

    # 2. Strip trailing code editor line numbers + boilerplate code + Run/Submit footer
    # Matches line numbers like "1 2 3 4 5 6 7 8 ⌄ ⌄ import java..." or editor code blocks at the end
    cleaned = re.sub(
        r'(?:\s+\b1\s+2\s+3(?:\s+\d+)*[\s⌄﹀∨v▲▼]*)(?:import\s+|#include|using\s+namespace|package\s+|public\s+class|class\s+\w+|def\s+\w+|function\s+\w+|const\s+\w+|let\s+\w+|var\s+\w+|int\s+main)[\s\S]*$',
        '',
        cleaned,
    ).strip()

    # Also strip trailing "Test1 Run Submit Input Output Expected Output" bar if still attached
    cleaned = re.sub(
        r'\s+\b(?:Test\s*1\s+)?Run\s+Submit(?:\s+Input\s+Output(?:\s+Expected\s+Output)?)?\.?\s*$',
        '',
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 3. Final check: if it still looks like pure navigation/overview and has no problem markers, reject it
    if is_navigation_or_overview_text(cleaned) and not has_problem_statement_markers(cleaned):
        return ""

    return cleaned


def extract_problem_title_from_text(text: str) -> Optional[str]:
    """Extract a clean problem title from a problem statement block if discernible."""
    if not text or not isinstance(text, str):
        return None
    cleaned = clean_extracted_problem_text(text) or text.strip()
    if not cleaned or is_navigation_or_overview_text(cleaned):
        return None

    # Check pattern: "<Title> (CAKEWALK|EASY|MEDIUM|HARD|BASIC|SCHOOL|BEGINNER|CHALLENGE) Description"
    m_diff = re.match(
        r'^([A-Z0-9][A-Za-z0-9\s\-_\(\)\[\],\.\'\+/]{1,65}?)\s+(?:CAKEWALK|EASY|MEDIUM|HARD|BASIC|SCHOOL|BEGINNER|EXPERT|CHALLENGE)\b',
        cleaned,
    )
    if m_diff:
        title = m_diff.group(1).strip()
        if title and not is_section_header_text(title) and len(title) <= 70:
            return title

    # Check pattern: "<Title> Description Given ..."
    m_desc = re.match(
        r'^([A-Z0-9][A-Za-z0-9\s\-_\(\)\[\]]{1,60}?)\s+Description\s+(?:Given|Write|You|Find|Print|Read|Calculate|Determine|Check|Complete|Implement|Create|Design)\b',
        cleaned,
    )
    if m_desc:
        title = m_desc.group(1).strip()
        if title and not is_section_header_text(title) and len(title) <= 70:
            return title

    # Check first line if multiline
    first_line = cleaned.splitlines()[0].strip()
    first_line = re.sub(r'\s+(?:CAKEWALK|EASY|MEDIUM|HARD|BASIC|SCHOOL)$', '', first_line, flags=re.IGNORECASE).strip()
    if 2 <= len(first_line) <= 65 and not is_section_header_text(first_line):
        if not re.match(r'^(?:given|write|you\s+are|find|print|input|output|description|problem\s+statement)\b', first_line, re.IGNORECASE):
            return first_line

    return None


def is_navigation_or_overview_text(text: str) -> bool:
    """Identify if extracted text is site chrome, chapter curriculum, or navigation rather than a problem statement."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 5:
        return False

    t_lower = t.lower()

    # 1. Check for chapter overview / curriculum patterns
    overview_patterns = [
        r'\b\d+\s+of\s+\d+\s+complete\b',           # e.g. "9 of 26 Complete"
        r'\bcomplete\s*\(\d+%\)',                   # e.g. "Complete (34%)"
        r'\b(?:previous|next)\s+chapter\b',         # e.g. "Previous Chapter: Making Decisions"
        r'\blearn\s+to\s+master\b',                 # e.g. "Learn to master The Power of Repetition"
        r'\btable\s+of\s+contents\b',
        r'\bcurriculum\s+overview\b',
        r'\bchapter\s+overview\b',
        r'\bmy\s+attendance\b',
        r'\bshow\s+qr\b',
        r'\bmy\s+doubts\b',
        r'\bmy\s+bookmarks\b',
        r'\bmy\s+orders\b',
    ]
    if any(re.search(p, t_lower) for p in overview_patterns):
        return True

    # 2. Check for site chrome keyword density
    chrome_keywords = [
        "home", "problems", "submissions", "blogs", "compete", "courses",
        "profile", "logout", "log out", "sign out", "theme", "account",
        "leaderboard", "discussions", "announcements", "syllabus"
    ]
    chrome_count = sum(1 for kw in chrome_keywords if re.search(r'\b' + re.escape(kw) + r'\b', t_lower))
    if chrome_count >= 3:
        return True

    # 3. Check if text starts with navbar sequence
    nav_prefix_pattern = r'^(?:[a-zA-Z0-9_-]+\s+)?(?:home|problems|courses|submissions|compete|blogs)\b'
    if re.search(nav_prefix_pattern, t_lower):
        return True

    return False


_is_navigation_or_overview_text = is_navigation_or_overview_text


async def extract_problem_from_dom(
    page, user_selector: Optional[str] = None
) -> tuple[str, list[dict], list[dict]]:
    """Directly extract problem statement and section questions from DOM via CDP.

    Returns:
        (extracted_text, section_questions, embedded_images)
    """
    if page is None:
        return "", [], []

    try:
        raw_eval = await page.evaluate(r"""(userSelector) => {
            function cleanText(text) {
                if (!text) return "";
                return text.replace(/\r\n/g, '\n')
                           .replace(/\t/g, ' ')
                           .split('\n')
                           .map(line => line.trimEnd())
                           .filter((line, idx, arr) => line.length > 0 || (idx > 0 && arr[idx - 1].length > 0))
                           .join('\n')
                           .trim();
            }

            function hasProblemMarkers(text) {
                if (!text || typeof text !== 'string') return false;
                const t = text.toLowerCase();
                let score = 0;
                if (/\binput\s+format\b/.test(t)) score += 2;
                if (/\boutput\s+format\b/.test(t)) score += 2;
                if (/\bexample\s+input\b|\bsample\s+input\b/.test(t)) score += 2;
                if (/\bexample\s+output\b|\bsample\s+output\b|\boutput\s+1\b/.test(t)) score += 2;
                if (/\bconstraints?\b/.test(t)) score += 2;
                if (/\bprohibited\s+keywords?\b/.test(t)) score += 2;
                if (/\bexplanation\b/.test(t)) score += 1;
                if (/\bgiven\s+(?:a|an|two|the|n)\b/.test(t)) score += 1;
                if (/\binput\s*:/.test(t) && /\boutput\s*:/.test(t)) score += 2;
                if (/\brefer\s+to\s+the\s+(?:below\s+)?image\b|\bdesign\s+a\b/.test(t)) score += 2;
                return score >= 3;
            }

            function isNavigationOrOverviewText(text) {
                if (!text || typeof text !== 'string') return false;
                const t = text.trim();
                if (t.length < 5) return false;
                const tLower = t.toLowerCase();

                const overviewPatterns = [
                    /\b\d+\s+of\s+\d+\s+complete\b/,
                    /\bcomplete\s*\(\d+%\)/,
                    /\b(?:previous|next)\s+chapter\b/,
                    /\blearn\s+to\s+master\b/,
                    /\btable\s+of\s+contents\b/,
                    /\bcurriculum\s+overview\b/,
                    /\bchapter\s+overview\b/,
                    /\bmy\s+attendance\b/,
                    /\bshow\s+qr\b/,
                    /\bmy\s+doubts\b/,
                    /\bmy\s+bookmarks\b/,
                    /\bmy\s+orders\b/
                ];
                if (overviewPatterns.some(p => p.test(tLower))) return true;

                const chromeKeywords = [
                    "home", "problems", "submissions", "blogs", "compete", "courses",
                    "profile", "logout", "log out", "sign out", "theme", "account",
                    "leaderboard", "discussions", "announcements", "syllabus"
                ];
                let count = 0;
                for (const kw of chromeKeywords) {
                    if (new RegExp('\\b' + kw + '\\b').test(tLower)) count++;
                    if (count >= 3) return true;
                }

                if (/^(?:[a-zA-Z0-9_-]+\s+)?(?:home|problems|courses|submissions|compete|blogs)\b/i.test(tLower)) {
                    return true;
                }

                return false;
            }

            function cleanProblemSlice(raw) {
                if (!raw || typeof raw !== 'string') return "";
                let cleaned = raw.trim();
                const mTabs = cleaned.match(/\bDescription\s+Submissions\s+(?:Editorial\s+|Solutions\s+|Hints\s+|Discuss(?:ions?)?\s+)*([A-Z0-9][\s\S]{20,})$/);
                if (mTabs) {
                    cleaned = mTabs[1].trim();
                } else {
                    const mNext = cleaned.match(/\bNext\s+Chapter\s*:\s*[^\n]+?(?:\.\.\.|\n)\s*([\s\S]{30,})$/i);
                    if (mNext && hasProblemMarkers(mNext[1])) {
                        cleaned = mNext[1].trim();
                    }
                }
                cleaned = cleaned.replace(/(?:\s+\b1\s+2\s+3(?:\s+\d+)*[\s⌄﹀∨v▲▼]*)(?:import\s+|#include|using\s+namespace|package\s+|public\s+class|class\s+\w+|def\s+\w+|function\s+\w+|const\s+\w+|let\s+\w+|var\s+\w+|int\s+main)[\s\S]*$/, '').trim();
                cleaned = cleaned.replace(/\s+\b(?:Test\s*1\s+)?Run\s+Submit(?:\s+Input\s+Output(?:\s+Expected\s+Output)?)?\.?\s*$/i, '').trim();
                if (isNavigationOrOverviewText(cleaned) && !hasProblemMarkers(cleaned)) {
                    return "";
                }
                return cleaned;
            }

            function extractWithImages(el, allowSlice = false) {
                if (!el) return null;
                let text = cleanText(el.innerText || el.textContent || '');
                if (text.length < 20) return null;
                if (isNavigationOrOverviewText(text)) {
                    if (allowSlice && hasProblemMarkers(text)) {
                        text = cleanProblemSlice(text);
                        if (!text || text.length < 20 || isNavigationOrOverviewText(text)) return null;
                    } else {
                        return null;
                    }
                }
                const blacklistRegex = /avatar|profile|gravatar|user[-_]?pic|user[-_]?icon|user[-_]?photo|user[-_]?image|account|author|member|person|portrait|logo|favicon|badge|banner-logo|header-img|nav-logo|topbar|sidebar-user|notification/i;

                function isBlacklisted(img) {
                    let curr = img;
                    while (curr && curr !== document.body) {
                        const tag = curr.tagName ? curr.tagName.toLowerCase() : '';
                        if (['nav', 'header', 'footer'].includes(tag)) return true;

                        const idStr = curr.id || '';
                        const classStr = typeof curr.className === 'string' ? curr.className : '';
                        const altStr = curr.alt || '';
                        const srcStr = curr.src || curr.getAttribute('src') || '';
                        const ariaStr = curr.getAttribute('aria-label') || '';

                        if (blacklistRegex.test(idStr) || blacklistRegex.test(classStr) || blacklistRegex.test(altStr) || blacklistRegex.test(srcStr) || blacklistRegex.test(ariaStr)) {
                            return true;
                        }
                        curr = curr.parentElement;
                    }
                    return false;
                }

                const imgs = Array.from(el.querySelectorAll('img'))
                    .map(img => ({
                        src: img.src || img.getAttribute('src') || '',
                        alt: img.alt || '',
                        width: img.naturalWidth || img.width || 0,
                        height: img.naturalHeight || img.height || 0,
                        blacklisted: isBlacklisted(img)
                    }))
                    .filter(i => i.src && i.src.startsWith('http') && !i.blacklisted && i.width >= 50 && i.height >= 50);
                return { text: text, images: imgs };
            }

            function queryRootsAll(selector) {
                const results = [];
                try {
                    results.push(...Array.from(document.querySelectorAll(selector)));
                } catch(e) {}
                try {
                    const all = document.querySelectorAll('*');
                    for (let i = 0; i < all.length; i++) {
                        if (all[i].shadowRoot) {
                            try {
                                results.push(...Array.from(all[i].shadowRoot.querySelectorAll(selector)));
                            } catch(e) {}
                        }
                    }
                } catch(e) {}
                try {
                    const iframes = document.querySelectorAll('iframe');
                    for (let i = 0; i < iframes.length; i++) {
                        try {
                            const doc = iframes[i].contentDocument || iframes[i].contentWindow?.document;
                            if (doc) {
                                results.push(...Array.from(doc.querySelectorAll(selector)));
                            }
                        } catch(e) {}
                    }
                } catch(e) {}
                return results;
            }

            if (userSelector && typeof userSelector === 'string' && userSelector.trim() && userSelector !== 'none' && userSelector !== 'null') {
                try {
                    const els = queryRootsAll(userSelector.trim());
                    for (const el of els) {
                        const result = extractWithImages(el);
                        if (result) return result;
                    }
                } catch(e) {}
            }

            const selectors = [
                '.mantine-AppShell-main .mantine-TypographyStylesProvider-root',
                'main .mantine-TypographyStylesProvider-root',
                '[class*="TypographyStylesProvider"]',
                '.mantine-AppShell-main [class*="TypographyStylesProvider"]',
                '.mantine-Tabs-panel[data-active="true"]',
                '[role="tabpanel"]',
                '.mantine-Tabs-panel',
                'div[data-track-load="description_content"]',
                'div[class*="description__"]',
                'div[class*="markdown"]',
                '.markdown-body',
                '.challenge-body-html',
                '.challenge-description',
                '.challenge-instructions',
                '.problem-description',
                '.question-statement',
                '.question-description',
                '.description-container',
                '.problem-statement',
                '#problem-description',
                '.question-body',
                '[class*="problem-description"]',
                '[class*="question-statement"]',
                '[class*="challenge-desc"]',
                '[data-cy="problem-statement"]',
                '[data-testid="problem-description"]',
                '.problem-content',
                '.statement-content',
                'article[class*="problem"]',
                '.mantine-ScrollArea-viewport'
            ];

            // Pass 1: Look for elements matching selectors that have strong problem statement markers and NO code editor inside
            for (const sel of selectors) {
                try {
                    const els = queryRootsAll(sel);
                    for (const el of els) {
                        if (el.closest('nav, header, footer, aside, .mantine-AppShell-navbar, #__omnitasker_panel')) continue;
                        if (el.querySelector('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor')) continue;
                        const result = extractWithImages(el);
                        if (result && hasProblemMarkers(result.text)) return result;
                    }
                } catch(e) {}
            }

            // Pass 2: Any element matching selectors that passes extractWithImages and doesn't wrap the code editor
            for (const sel of selectors) {
                try {
                    const els = queryRootsAll(sel);
                    for (const el of els) {
                        if (el.closest('nav, header, footer, aside, .mantine-AppShell-navbar, #__omnitasker_panel')) continue;
                        if (el.querySelector('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor')) continue;
                        const result = extractWithImages(el);
                        if (result) return result;
                    }
                } catch(e) {}
            }

            // Pass 3: Score all content containers to find the tightest problem description container
            let bestProblemElem = null;
            let bestScore = -1;
            let bestLen = Infinity;
            try {
                const allElems = document.querySelectorAll('div, section, article, main, [role="tabpanel"]');
                for (const elem of allElems) {
                    if (elem.closest('nav, header, footer, aside, [class*="Navbar" i], [class*="sidebar" i], .mantine-AppShell-navbar, #__omnitasker_panel')) {
                        continue;
                    }
                    if (elem.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, textarea')) {
                        continue;
                    }
                    // Exclude parent split-pane wrappers that also wrap the code editor
                    if (elem.querySelector('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor')) {
                        continue;
                    }
                    const rawTxt = cleanText(elem.innerText || elem.textContent || '');
                    if (rawTxt.length < 40 || rawTxt.length > 20000) continue;
                    if (isNavigationOrOverviewText(rawTxt)) continue;

                    const t = rawTxt.toLowerCase();
                    let score = 0;
                    if (/\binput\s+format\b/.test(t)) score += 3;
                    if (/\boutput\s+format\b/.test(t)) score += 3;
                    if (/\bexample\s+input\b|\bsample\s+input\b/.test(t)) score += 3;
                    if (/\bconstraints?\b/.test(t)) score += 3;
                    if (/\bdescription\b/.test(t)) score += 2;
                    if (/\bexplanation\b/.test(t)) score += 1;
                    if (/\bgiven\s+(?:a|an|two|the|n)\b/.test(t)) score += 2;

                    if (score > bestScore || (score === bestScore && score > 0 && rawTxt.length < bestLen && rawTxt.length >= 60)) {
                        bestScore = score;
                        bestLen = rawTxt.length;
                        bestProblemElem = elem;
                    }
                }
                if (bestProblemElem && bestScore >= 2) {
                    const result = extractWithImages(bestProblemElem);
                    if (result) return result;
                }
            } catch(e) {}

            // Pass 4: Fallback to main/body with cleanProblemSlice
            try {
                const mainElems = queryRootsAll('.mantine-AppShell-main, main, [role="main"], article');
                for (const mainElem of mainElems) {
                    const res = extractWithImages(mainElem, true);
                    if (res) return res;
                }

                const bodyClone = document.body.cloneNode(true);
                const tagsToRemove = [
                    'script', 'style', 'nav', 'header', 'footer', 'aside', 'svg', 'iframe', 'noscript',
                    '.mantine-Header-root', '.mantine-AppShell-header', '[class*="Header" i]', '[class*="header" i]',
                    '[class*="Topbar" i]', '[class*="topbar" i]',
                    '.mantine-Navbar-root', '.mantine-AppShell-navbar', '[class*="navbar" i]', '[class*="Navbar" i]',
                    '[class*="sidebar" i]', '[class*="Sidebar" i]',
                    '.mantine-Menu-dropdown', '.mantine-Menu-root', '[role="menu"]', '[role="navigation"]',
                    '[class*="breadcrumb" i]', '[class*="Breadcrumb" i]',
                    '.monaco-editor', '.CodeMirror', '.ace_editor', '.cm-editor',
                    '#__omnitasker_panel'
                ];
                for (const tag of tagsToRemove) {
                    try {
                        const elems = bodyClone.querySelectorAll(tag);
                        for (const el of elems) el.remove();
                    } catch(e) {}
                }
                const bodyFallback = cleanProblemSlice(cleanText(bodyClone.innerText || bodyClone.textContent || ''));
                if (bodyFallback.length > 0 && !isNavigationOrOverviewText(bodyFallback)) {
                    return { text: bodyFallback, images: [] };
                }
                const finalFallback = cleanProblemSlice(cleanText(document.body ? (document.body.innerText || document.body.textContent || '') : ''));
                if (finalFallback.length > 0 && !isNavigationOrOverviewText(finalFallback)) {
                    return { text: finalFallback, images: [] };
                }
                return { text: "", images: [] };
            } catch(e) {
                return { text: "", images: [] };
            }
        }""", user_selector)
        extracted_data = parse_eval_result(raw_eval)
    except Exception as e:
        extracted_data = None

    raw_text = ""
    embedded_images = []
    if isinstance(extracted_data, dict):
        raw_text = extracted_data.get("text", "")
        embedded_images = extracted_data.get("images", [])

    raw_text = clean_extracted_problem_text(raw_text)
    active_problem_title = extract_problem_title_from_text(raw_text) or ""

    # Section questions navigation scanner
    questions = []
    try:
        # Auto-expand collapsed Mantine accordions (.mantine-NavLink-root) containing "practice" or "problem" or chevron icons so child questions are discovered
        expand_script = r"""() => {
            let expandedAny = false;
            try {
                const accordions = Array.from(document.querySelectorAll(
                    '.mantine-Accordion-control, button[class*="Accordion-control"], [data-accordion-control], ' +
                    '.mantine-NavLink-root, button[class*="NavLink"], a[class*="NavLink"], [role="treeitem"], ' +
                    '[aria-expanded], [data-expanded], [data-opened], ' +
                    'button[class*="collapse" i], button[class*="accordion" i], ' +
                    '[data-bs-toggle="collapse"], [data-toggle="collapse"], ' +
                    'details:not([open]) summary, [class*="section-header" i], [class*="chapter-header" i]'
                ));
                for (const acc of accordions) {
                    const txt = (acc.innerText || '').toLowerCase().trim();
                    const hasChevron = acc.querySelector(
                        '.mantine-NavLink-chevron, [data-chevron], [class*="chevron" i], [class*="caret" i], [class*="arrow" i], [class*="Accordion-chevron"]'
                    ) !== null || /chevron|caret|arrow/i.test(acc.innerHTML || '');
                    const isAccordionControl = acc.classList.contains('mantine-Accordion-control') ||
                                               acc.hasAttribute('data-accordion-control') ||
                                               /accordion-control|accordion-button|collapse-toggle/i.test(acc.className || '');
                    const hasAriaExpanded = acc.hasAttribute('aria-expanded');
                    const next = acc.nextElementSibling;
                    const hasPanelSibling = next && (
                        next.classList.contains('mantine-NavLink-children') ||
                        next.classList.contains('mantine-Accordion-panel') ||
                        next.classList.contains('mantine-Collapse-root') ||
                        (next.className && typeof next.className === 'string' && /children|panel|collapse|body|content|list/i.test(next.className))
                    );
                    const isCategoryTitle = /^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)?(practice\s*problems?|practice\s*questions?|problem\s*set|exercises?|assignments?|topics?|modules?|chapters?|sections?|challenges?|contests?|assessments?|homework|tutorials?|labs?|units?|lessons?|milestones?|sessions?)(?:[\s:\-–—\d\(\)\[\]#]+.*)?$/i.test(txt) ||
                                            /^(chapter|module|section|week|day|part|unit|lesson|milestone|session|lab|topic|level)\s*[\d#]+(?:[\s:\-–—]+.*)?$/i.test(txt) ||
                                            /^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)(problems?|questions?|exercises?|challenges?|tasks?)$/i.test(txt) ||
                                            /(\d+\s*problems?|\d+\s*questions?|\d+\s*tasks?|\d+\s*items?)/i.test(txt);

                    // Only consider expandable containers, not leaf question items
                    const isExpandable = isAccordionControl || hasChevron || (hasAriaExpanded && acc.getAttribute('aria-expanded') !== null) || hasPanelSibling || isCategoryTitle;
                    if (!isExpandable) continue;

                    let isExpanded = false;
                    if (acc.getAttribute('aria-expanded') === 'false' || acc.getAttribute('data-expanded') === 'false' || acc.getAttribute('data-opened') === 'false') {
                        isExpanded = false;
                    } else if (acc.getAttribute('aria-expanded') === 'true' ||
                               acc.getAttribute('data-expanded') === 'true' ||
                               acc.getAttribute('data-opened') === 'true' ||
                               acc.classList.contains('mantine-NavLink-opened') ||
                               acc.classList.contains('mantine-Accordion-control-opened') ||
                               acc.classList.contains('opened')) {
                        isExpanded = true;
                    } else if (hasPanelSibling && next) {
                        if (next.offsetHeight > 0 && next.style.display !== 'none' && next.style.height !== '0px' && !next.hidden && next.getAttribute('aria-hidden') !== 'true') {
                            isExpanded = true;
                        }
                    }
                    if (!isExpanded && next && next.querySelector) {
                        const childPanel = next.querySelector('.mantine-NavLink-children, .mantine-Accordion-panel, .mantine-Collapse-root');
                        if (childPanel && childPanel.offsetHeight > 0 && childPanel.style.display !== 'none' && !childPanel.hidden) {
                            isExpanded = true;
                        }
                    }
                    const itemContainer = acc.closest('.mantine-Accordion-item, [class*="Accordion-item"]');
                    if (!isExpanded && itemContainer && itemContainer.querySelector('.mantine-Accordion-control, button[class*="Accordion-control"]') === acc) {
                        const itemPanel = itemContainer.querySelector('.mantine-Accordion-panel');
                        if (itemPanel && itemPanel.offsetHeight > 0 && itemPanel.style.display !== 'none' && !itemPanel.hidden) {
                            isExpanded = true;
                        }
                    }

                    if (!isExpanded) {
                        try {
                            acc.scrollIntoView({ block: 'nearest' });
                            acc.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                            acc.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                            acc.click();
                            expandedAny = true;
                        } catch(e) {}
                    }
                }
            } catch(e) {}
            return expandedAny;
        }"""
        for pass_i in range(2):
            expanded_raw = await page.evaluate(expand_script)
            if parse_eval_result(expanded_raw) is True:
                await asyncio.sleep(0.5)
            else:
                break

        raw_nav = await page.evaluate(r"""(activeProblemTitle) => {
            const selectors = [
                '.mantine-NavLink-children .mantine-NavLink-root',
                '.mantine-NavLink-children a',
                '.mantine-NavLink-children button',
                '.mantine-Collapse-root .mantine-NavLink-root',
                '.mantine-Collapse-root a',
                '.mantine-Collapse-root button',
                '.mantine-Accordion-panel .mantine-NavLink-root',
                '.mantine-Accordion-panel a',
                '.mantine-Accordion-panel button',
                '.mantine-AppShell-main a[href*="chapter"]',
                '.mantine-AppShell-main a[href*="active="]',
                '.mantine-AppShell-main [class*="Card"] a',
                '.mantine-AppShell-main [class*="curriculum" i] a',
                'a[href*="active="]', 'a[href*="chapter"]', 'a[href*="problem"]',
                '[class*="question-list"] li', '[class*="question-item"]',
                '[class*="questionItem"]', '[class*="problem-item"]',
                '[class*="challenge-item"]', '[class*="task-item"]',
                '.mantine-NavLink-root', 'button[class*="NavLink"]',
                '.question-tab', 'li.question',
                '.sidebar-item', '[data-cy*="question"]'
            ];
            let candidateElems = Array.from(document.querySelectorAll(selectors.join(',')));
            if (candidateElems.length === 0) {
                // Generic fallback must represent navigation controls. Static
                // headings/list content are not tasks, and a substring such as
                // "item" also matches unrelated layout classes (items-center).
                candidateElems = Array.from(document.querySelectorAll('a[href], button, [role="tab"], [role="link"]'));
            }
            const qList = [];
            const seen = new Set();
            const activeTitleLower = (activeProblemTitle || '').toLowerCase().trim();
            const navKeywords = /^(question|task|problem|assignment|coding|practice|challenge|q\d+|\d+[\.:\)\s]?|[0-9]{1,2}$)/i;
            const nonCodingKeywords = /^(?:intro|introduction|overview|summary|video|reading|lecture|welcome|index|setup|getting\s+started|instructions|home|problems|courses?|submissions?|blogs?|compete|code|my\s+doubts?|application|profile|my\s+bookmarks?|my\s+orders?|my\s+attendance|show\s+qr|theme|account|logout|log\s+out|sign\s+out|login|sign\s+in|previous\s+chapter.*|next\s+chapter.*|back\s+to\s+course|course\s+home|description|editorial|solutions?|hints?|discuss(?:ions?)?|test\s*\d*|run|submit|input|output|expected\s+output|custom\s+input|console)$/i;
            const isSectionTitle = (t) => {
                const sectionPatterns = [
                    /^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)?(practice\s*problems?|practice\s*questions?|problem\s*set|exercises?|assignments?|topics?|modules?|chapters?|sections?|challenges?|contests?|assessments?|homework|tutorials?|labs?|units?|lessons?|milestones?|sessions?)(?:[\s:\-–—\d\(\)\[\]#]+.*)?$/i,
                    /^(chapter|module|section|week|day|part|unit|lesson|milestone|session|lab|topic|level)\s*[\d#]+(?:[\s:\-–—]+.*)?$/i,
                    /^(?:(?:l|lab|lesson|ch|chapter|sec|section|mod|module|part|p|week|w|day|d|unit|u|topic)\s*[\d#]+[\s:\-–—]+|[a-z]?\d+[\s:\-–—]+)(problems?|questions?|exercises?|challenges?|tasks?)$/i,
                    /^(all\s*problems?|problem\s*list|question\s*list|all\s*questions|practice|problems|questions|challenges)$/i,
                    /(\d+\s*problems?|\d+\s*questions?|\d+\s*tasks?|\d+\s*items?|\d+\/\d+\s*completed)/i,
                    /^(arrays?|strings?|linked\s*lists?|stacks?(?:\s*and\s*queues?)?|queues?|trees?(?:\s*and\s*graphs?)?|graphs?|dynamic\s*programming|dp|recursion(?:\s*and\s*backtracking)?|backtracking|binary\s*search|greedy(?:\s*algorithms?)?|bit\s*manipulation|sorting(?:\s*and\s*searching)?|math(?:\s*and\s*logic)?|two\s*pointers|sliding\s*window|hash\s*tables?|heaps?|tries?)(?:[\s:\-–—\(\)\[\]\d]+.*)?$/i
                ];
                return sectionPatterns.some(p => p.test(t));
            };

            function hasGreenCompletionIndicator(el) {
                try {
                    if (el.classList.contains('completed') || el.classList.contains('passed') || el.classList.contains('solved')) return true;
                    if (el.querySelector('.completed, .passed, .solved, svg[data-icon*="check" i], [class*="check" i], [class*="success" i], [data-color="green"], [data-color="teal"]')) return true;
                    const html = el.innerHTML || '';
                    if (/✓|✔|completed|passed|solved|tabler-icon-check|tabler-icon-circle-check|icon-check|--mantine-color-green|--mantine-color-teal|M5\s+12l5\s+5l10\s+-10|M9\s+12l2\s+2l4\s+-4|M20\s+6L9\s+17l-5-5/i.test(html)) {
                        return true;
                    }
                    const iconNodes = el.querySelectorAll('svg, path, [class*="Icon" i], [class*="icon" i], [class*="Section" i], span');
                    for (const node of iconNodes) {
                        const st = window.getComputedStyle ? window.getComputedStyle(node) : null;
                        if (!st) continue;
                        for (const prop of [st.color, st.stroke, st.fill, st.backgroundColor]) {
                            if (!prop) continue;
                            const m = prop.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                            if (m) {
                                const r = parseInt(m[1], 10), g = parseInt(m[2], 10), b = parseInt(m[3], 10);
                                if (g >= 120 && g > r * 1.3 && g > b * 1.15) return true;
                            }
                        }
                    }
                } catch(e) {}
                return false;
            }

            for (const el of candidateElems) {
                // Skip site headers, topbars, user profile dropdowns, footers, code editors
                if (el.closest('header, footer, .mantine-AppShell-header, .mantine-Header-root, [class*="Header" i], [class*="header" i], [class*="Topbar" i], [class*="topbar" i], .mantine-Menu-dropdown, .monaco-editor, .CodeMirror, .ace_editor, #__omnitasker_panel')) {
                    continue;
                }

                // Skip accordion headers or parent items with chevrons/expand indicators or child NavLinks
                // Note: Do NOT blindly treat any SVG inside .mantine-NavLink-rightSection as a chevron, since leaf items put completion checkmarks there!
                const hasChevron = el.querySelector(
                    '.mantine-NavLink-chevron, [data-chevron], [class*="chevron" i], [class*="caret" i], [class*="arrow" i], [class*="Accordion-chevron"]'
                ) !== null || /tabler-icon-chevron|icon-chevron|fa-chevron|chevron-right|chevron-down/i.test(el.innerHTML || '');
                const isAccordionControl = el.classList.contains('mantine-Accordion-control') ||
                                           el.hasAttribute('data-accordion-control') ||
                                           el.hasAttribute('aria-expanded') ||
                                           /accordion-control|accordion-button|collapse-toggle/i.test(el.className || '');
                const hasChildrenSibling = (function() {
                    const next = el.nextElementSibling;
                    return next && (next.classList.contains('mantine-NavLink-children') ||
                                    next.classList.contains('mantine-Accordion-panel') ||
                                    next.classList.contains('mantine-Collapse-root') ||
                                    (next.className && typeof next.className === 'string' && (next.className.includes('children') || next.className.includes('panel') || next.className.includes('collapse'))));
                })();
                const hasChildNavLinks = el.querySelector('.mantine-NavLink-root, button[class*="NavLink"], .mantine-NavLink-children, .mantine-Collapse-root') !== null;

                if (hasChevron || isAccordionControl || hasChildrenSibling || hasChildNavLinks) {
                    continue;
                }
                const labelEl = el.querySelector('.mantine-NavLink-label, [class*="label"]') || el;
                const txt = (labelEl.innerText || el.innerText || '').trim().replace(/\s+/g, ' ');
                if (!txt || txt.length > 90 || seen.has(txt) || nonCodingKeywords.test(txt)) continue;

                // Section title check: skip section/category headers unconditionally
                if (isSectionTitle(txt)) {
                    continue;
                }

                const href = (el.getAttribute('href') || '') + ' ' + (el.querySelector('a')?.getAttribute('href') || '');
                const hasQuestionUrl = href.includes('active=') || (href.includes('chapter') && !href.includes('previous') && !href.includes('next')) || href.includes('problem');
                const isInsideChildren = el.closest('.mantine-NavLink-children, .mantine-Collapse-root, .mantine-Accordion-panel') !== null;
                const isSidebarChild = el.closest('[class*="sidebar" i], [class*="playlist" i], [class*="question" i], [class*="Navbar" i], .mantine-AppShell-navbar, aside') !== null;
                const isCurriculumItem = el.closest('.mantine-AppShell-main, main, [role="main"]') !== null && (hasQuestionUrl || navKeywords.test(txt));

                if (navKeywords.test(txt) || hasQuestionUrl || isInsideChildren || (isSidebarChild && txt.length >= 1 && txt.length < 90) || isCurriculumItem) {
                    seen.add(txt);
                    const isCompleted = hasGreenCompletionIndicator(el);
                    const txtLower = txt.toLowerCase();
                    const isActive = el.getAttribute('data-active') === 'true' ||
                                     el.getAttribute('aria-current') === 'page' ||
                                     el.getAttribute('aria-selected') === 'true' ||
                                     el.classList.contains('active') ||
                                     el.classList.contains('mantine-NavLink-active') ||
                                     (activeTitleLower.length >= 3 && txtLower === activeTitleLower);
                    qList.push({
                        text: txt,
                        completed: isCompleted,
                        active: isActive,
                        insideChildPanel: isInsideChildren
                    });
                }
            }
            // If we found leaf questions inside an expanded accordion/child panel (.mantine-NavLink-children / .mantine-Accordion-panel),
            // prefer those leaf questions over unexpanded sibling section items outside the panel
            let finalList = qList;
            const childPanelItems = qList.filter(q => q.insideChildPanel && !isSectionTitle(q.text));
            if (childPanelItems.length >= 1) {
                finalList = childPanelItems;
            } else if (qList.length > 1) {
                const filtered = qList.filter(q => !isSectionTitle(q.text));
                if (filtered.length > 0) finalList = filtered;
            }
            const activeIdx = finalList.findIndex(q => q.active);
            if (activeIdx > 0 && !finalList.some(q => q.completed)) {
                const bodyTxt = document.body ? (document.body.innerText || '') : '';
                const mComp = bodyTxt.match(/(\d+)\s+of\s+(\d+)\s+complete/i);
                if (mComp) {
                    const compCount = parseInt(mComp[1], 10);
                    const totalCount = parseInt(mComp[2], 10);
                    const remaining = totalCount - compCount;
                    if (remaining > 0 && (finalList.length - activeIdx) === remaining) {
                        for (let i = 0; i < activeIdx; i++) {
                            finalList[i].completed = true;
                        }
                    }
                }
            }
            return finalList;
        }""", active_problem_title)
        nav_data = parse_eval_result(raw_nav)
        if isinstance(nav_data, list):
            questions = nav_data
    except Exception as e:
        questions = []

    return raw_text, questions, embedded_images


_extract_problem_from_dom_direct = extract_problem_from_dom


async def inject_code_to_editor(page, code: str) -> Optional[str]:
    """Directly inject solution code into Monaco, CodeMirror, Ace, Contenteditable, or Textarea editors on the active page."""
    if page is None:
        return None

    js_script = """(code) => {
        function setEditorCodeRecursively(doc, code) {
            if (!doc) return null;
            const win = doc.defaultView || window;

            // 1. Monaco Editor (VS Code based)
            try {
                if (win.monaco && win.monaco.editor) {
                    if (win.monaco.editor.getEditors) {
                        const eds = win.monaco.editor.getEditors();
                        for (const ed of eds) {
                            try {
                                if (ed.getDomNode() && ed.getDomNode().offsetParent !== null) {
                                    ed.setValue(code);
                                    return "Monaco Editor (active editor) updated in " + doc.location.href;
                                }
                            } catch(e) {}
                        }
                        if (eds.length > 0 && eds[0].getModel()) {
                            eds[0].getModel().setValue(code);
                            return "Monaco Editor (first editor) updated in " + doc.location.href;
                        }
                    }
                    const models = win.monaco.editor.getModels();
                    if (models && models.length > 0) {
                        const validModels = models.filter(m => m.uri && !m.uri.path.endsWith('.d.ts'));
                        const targetModel = validModels.length > 0 ? validModels[0] : models[0];
                        targetModel.setValue(code);
                        return "Monaco Editor (model) updated in " + doc.location.href;
                    }
                }
            } catch(e) {}

            // Custom window editor properties
            for (const k of ['editor', 'monacoEditor', 'codeEditor']) {
                try {
                    if (win[k] && typeof win[k].setValue === 'function') {
                        win[k].setValue(code);
                        return "Custom editor (" + k + ") updated in " + doc.location.href;
                    }
                } catch(e) {}
            }

            // Bundled Monaco DOM fallback via inputarea
            try {
                const monacoEls = Array.from(doc.querySelectorAll('.monaco-editor'));
                for (const mEl of monacoEls) {
                    if (mEl.offsetParent !== null || monacoEls.length === 1) {
                        const ta = mEl.querySelector('textarea.inputarea');
                        if (ta) {
                            ta.focus();
                            doc.execCommand('selectAll', false, null);
                            if (doc.execCommand('insertText', false, code)) {
                                return "Monaco Editor (execCommand) updated in " + doc.location.href;
                            }
                            const dt = new DataTransfer();
                            dt.setData('text/plain', code);
                            const pasteEv = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                            ta.dispatchEvent(pasteEv);
                            return "Monaco Editor (paste event) updated in " + doc.location.href;
                        }
                    }
                }
            } catch(e) {}

            // 2. CodeMirror (v5)
            try {
                const cmElems = Array.from(doc.querySelectorAll('.CodeMirror'));
                cmElems.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                for (const elem of cmElems) {
                    if (elem.CodeMirror) {
                        elem.CodeMirror.setValue(code);
                        if (elem.CodeMirror.save) elem.CodeMirror.save();
                        return "CodeMirror v5 code updated in " + doc.location.href;
                    }
                }
            } catch(e) {}

            // 3. CodeMirror 6
            try {
                const cmEditors = Array.from(doc.querySelectorAll('.cm-editor'));
                cmEditors.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                for (const cmEditor of cmEditors) {
                    const view = (cmEditor.cmView && (cmEditor.cmView.view || (typeof cmEditor.cmView.dispatch === 'function' ? cmEditor.cmView : null)));
                    if (view && typeof view.dispatch === 'function') {
                        view.dispatch({
                            changes: { from: 0, to: view.state.doc.length, insert: code }
                        });
                        return "CodeMirror 6 dispatch updated in " + doc.location.href;
                    }
                }
                const cmContents = Array.from(doc.querySelectorAll('.cm-content'));
                cmContents.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                for (const el of cmContents) {
                    const view = (el.cmView && (el.cmView.view || (typeof el.cmView.dispatch === 'function' ? el.cmView : null)));
                    if (view && typeof view.dispatch === 'function') {
                        view.dispatch({
                            changes: { from: 0, to: view.state.doc.length, insert: code }
                        });
                        return "CodeMirror 6 content dispatch updated in " + doc.location.href;
                    }
                    try {
                        el.focus();
                        const sel = win.getSelection();
                        const range = doc.createRange();
                        range.selectNodeContents(el);
                        sel.removeAllRanges();
                        sel.addRange(range);
                        if (doc.execCommand('insertText', false, code)) {
                            return "CodeMirror 6 execCommand updated in " + doc.location.href;
                        }
                        const dt = new DataTransfer();
                        dt.setData('text/plain', code);
                        const pasteEv = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                        el.dispatchEvent(pasteEv);
                        return "CodeMirror 6 paste event updated in " + doc.location.href;
                    } catch(e) {}
                }
            } catch(e) {}

            // 4. Ace Editor
            try {
                const aceElems = Array.from(doc.querySelectorAll('.ace_editor'));
                aceElems.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                for (const elem of aceElems) {
                    if (win.ace && win.ace.edit) {
                        const editor = win.ace.edit(elem);
                        if (editor) {
                            editor.setValue(code, -1);
                            return "Ace Editor code updated in " + doc.location.href;
                        }
                    }
                    if (elem.env && elem.env.editor) {
                        elem.env.editor.setValue(code, -1);
                        return "Ace Editor (env) code updated in " + doc.location.href;
                    }
                }
            } catch(e) {}

            // 5. Standard Textarea
            try {
                const textareas = Array.from(doc.querySelectorAll('textarea')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                textareas.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                if (textareas.length > 0) {
                    const elem = textareas[0];
                    elem.focus();
                    elem.value = code;
                    elem.dispatchEvent(new Event('input', { bubbles: true }));
                    elem.dispatchEvent(new Event('change', { bubbles: true }));
                    return "Textarea value updated in " + doc.location.href;
                }
            } catch(e) {}

            // 6. Contenteditable
            try {
                const ces = Array.from(doc.querySelectorAll('[contenteditable="true"]')).filter(
                    el => !el.closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor, #__omnitasker_panel')
                );
                ces.sort((a, b) => {
                    const rA = a.getBoundingClientRect();
                    const rB = b.getBoundingClientRect();
                    return (rB.width * rB.height) - (rA.width * rA.height);
                });
                if (ces.length > 0) {
                    const elem = ces[0];
                    elem.focus();
                    try {
                        const sel = win.getSelection();
                        const range = doc.createRange();
                        range.selectNodeContents(elem);
                        sel.removeAllRanges();
                        sel.addRange(range);
                        if (doc.execCommand('insertText', false, code)) {
                            return "Contenteditable execCommand updated in " + doc.location.href;
                        }
                    } catch(e) {}
                    elem.innerText = code;
                    elem.dispatchEvent(new Event('input', { bubbles: true }));
                    elem.dispatchEvent(new Event('change', { bubbles: true }));
                    return "Contenteditable updated in " + doc.location.href;
                }
            } catch(e) {}

            // 7. Drill into same-origin iframes
            try {
                const iframes = doc.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    try {
                        const iframeDoc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
                        if (iframeDoc) {
                            const res = setEditorCodeRecursively(iframeDoc, code);
                            if (res) return res;
                        }
                    } catch(e) {}
                }
            } catch(e) {}

            return null;
        }
        return setEditorCodeRecursively(document, code);
    }"""

    injected = None
    try:
        injected = await page.evaluate(js_script, code)
    except Exception as e:
        print(f"JS code injection evaluation note: {e}")

    if not injected:
        try:
            fallback_script = """(code) => {
                for (const selector of [".monaco-editor textarea.inputarea", ".CodeMirror", ".ace_editor", ".cm-content", "[contenteditable='true']", "textarea"]) {
                    const el = document.querySelector(selector);
                    if (el && el.offsetParent !== null) {
                        try {
                            el.focus();
                            if ('value' in el) {
                                el.value = code;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                return "Editor fallback updated via value: " + selector;
                            }
                            const sel = window.getSelection();
                            const range = document.createRange();
                            range.selectNodeContents(el);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            if (document.execCommand('insertText', false, code)) {
                                return "Editor fallback updated via execCommand: " + selector;
                            }
                            el.innerText = code;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            return "Editor fallback updated via innerText: " + selector;
                        } catch(e) {}
                    }
                }
                return null;
            }"""
            injected = await page.evaluate(fallback_script, code)
        except Exception as fb_err:
            print(f"Direct injection DOM fallback note: {fb_err}")

    return injected


async def extract_ref_images_to_disk(
    browser: Any,
    page: Any,
    output_dir: Optional[Path] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> list[dict]:
    """
    Extract all reference/mockup images from the current page and save them
    to ref_images/ using Playwright's native screenshot_element.
    Filters out avatars, profile pictures, icons, and UI chrome logos.
    Returns a list of dicts with keys: index, alt, src, file_path.
    """
    def _log(msg: str, level: str = "info") -> None:
        if log_fn:
            try:
                log_fn(msg, level)
            except Exception:
                pass

    if page is None:
        _log("⚠️ No active page available for reference image extraction.", "warn")
        return []

    # Find candidate mockup images with strict profile picture & UI chrome blacklist filtering
    extracted_data_str = await page.evaluate("""() => {
        function getPath(el) {
            if (el.id) return '#' + el.id;
            if (el === document.body) return 'body';
            const parent = el.parentElement;
            if (!parent) return el.tagName.toLowerCase();
            const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
            const idx = siblings.indexOf(el);
            const tag = el.tagName.toLowerCase();
            return getPath(parent) + ' > ' + tag + (siblings.length > 1 ? ':nth-of-type(' + (idx+1) + ')' : '');
        }

        const blacklistRegex = /avatar|profile|gravatar|user[-_]?pic|user[-_]?icon|user[-_]?photo|user[-_]?image|account|author|member|person|portrait|logo|favicon|badge|banner-logo|header-img|nav-logo|topbar|sidebar-user|notification/i;

        function isBlacklisted(img) {
            let curr = img;
            while (curr && curr !== document.body) {
                const tag = curr.tagName ? curr.tagName.toLowerCase() : '';
                if (['nav', 'header', 'footer'].includes(tag)) return true;

                const idStr = curr.id || '';
                const classStr = typeof curr.className === 'string' ? curr.className : '';
                const altStr = curr.alt || '';

                if (blacklistRegex.test(idStr) || blacklistRegex.test(classStr) || blacklistRegex.test(altStr)) return true;
                curr = curr.parentElement;
            }
            return false;
        }

        const imgs = Array.from(document.querySelectorAll('img'));
        const results = imgs.map(img => {
            const rect = img.getBoundingClientRect();
            const isVisible = rect.width > 0 && rect.height > 0 && img.offsetParent !== null;
            const blacklisted = isBlacklisted(img);

            let inProblem = false;
            let p = img.parentElement;
            while (p && p !== document.body) {
                const cls = typeof p.className === 'string' ? p.className : '';
                const id = p.id || '';
                if (cls.includes('problem') || cls.includes('description') || cls.includes('statement') || cls.includes('question') ||
                    id.includes('problem') || id.includes('description') || id.includes('statement') || id.includes('question')) {
                    inProblem = true;
                    break;
                }
                p = p.parentElement;
            }

            return {
                src: img.src || img.getAttribute('data-src') || '',
                alt: img.alt || '',
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                cssPath: getPath(img),
                blacklisted: blacklisted,
                inProblem: inProblem,
                visible: isVisible
            };
        }).filter(i => {
            if (!i.src || !i.src.startsWith('http')) return false;
            if (i.blacklisted) return false;
            if (i.width === i.height && i.width < 100 && !i.inProblem) return false;
            if (i.width < 50 || i.height < 50) return false;
            return true;
        });

        results.sort((a, b) => (b.inProblem ? 1 : 0) - (a.inProblem ? 1 : 0));
        return JSON.stringify(results);
    }""")

    try:
        candidates = json.loads(extracted_data_str)
    except Exception as e:
        _log(f"Failed to parse extracted images JSON: {e}", "warn")
        candidates = []

    if not candidates:
        _log("No reference images (>= 50px, excluding profile pictures/logos) found on the page.", "warn")
        return []

    _log(f"Found {len(candidates)} candidate reference mockup images (filtered for privacy). Capturing...", "info")

    ref_dir = output_dir or Path("D:/auto/ref_images")
    os.makedirs(str(ref_dir), exist_ok=True)
    saved_paths = []

    async def capture_candidate(idx, item):
        css_path = item.get("cssPath")
        src = item.get("src", "")
        alt = item.get("alt", "")

        if not css_path:
            return None

        save_path = os.path.join(str(ref_dir), f"ref_image_{idx+1}.png")
        _log(f"Screenshotting reference mockup image {idx+1}: {alt or src[:60]}...", "info")

        try:
            # Scroll element into view
            await page.evaluate("""(selector) => {
                const el = document.querySelector(selector);
                if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
            }""", css_path)
            await asyncio.sleep(0.5)

            # Primary: Playwright-native element screenshot (bypasses CORS)
            if browser and hasattr(browser, "screenshot_element"):
                raw_bytes = await browser.screenshot_element(css_path, path=save_path, format="png")
                if raw_bytes:
                    _log(f"Mockup image {idx+1} saved to {save_path}", "success")
                    return {
                        "index": idx + 1,
                        "alt": alt,
                        "src": src,
                        "file_path": save_path,
                    }
        except Exception as img_err:
            _log(f"screenshot_element failed for {css_path}: {img_err}. Trying fetch fallback...", "warn")

        # Fallback: download image via JS fetch and save manually
        try:
            b64_data = await page.evaluate("""(url) => (async () => {
                try {
                    const resp = await fetch(url);
                    const blob = await resp.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            })()""", src)
            if b64_data:
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                _log(f"Mockup image {idx+1} saved via fetch fallback to {save_path}", "success")
                return {
                    "index": idx + 1,
                    "alt": alt,
                    "src": src,
                    "file_path": save_path,
                }
        except Exception as fetch_err:
            _log(f"Fetch fallback also failed for image {idx+1}: {fetch_err}", "warn")
        return None

    for idx, item in enumerate(candidates[:10]):
        res = await capture_candidate(idx, item)
        if res:
            saved_paths.append(res)

    return saved_paths
