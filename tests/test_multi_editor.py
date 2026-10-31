"""
Unit tests for multi-editor detection, language classification,
and targeted code injection in HTML/CSS problem workspaces.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from editor_adapters import (
    MonacoAdapter,
    safe_write,
    classify_code_language,
    split_multi_editor_code,
    get_all_editors_summary,
)


class TestMultiEditor(unittest.TestCase):
    def test_classify_code_language(self):
        css_sample = "body {\n  margin: 0;\n  padding: 0;\n  background-color: #f5f5f5;\n  display: flex;\n}"
        html_sample = "<!DOCTYPE html>\n<html>\n<head><title>App</title></head>\n<body>\n<div class=\"app\">Hello</div>\n</body>\n</html>"
        html_snippet = "<div class=\"card\">\n  <h2>Title</h2>\n</div>"
        js_sample = "function calculateTotal(items) {\n  let total = 0;\n  return total;\n}"

        self.assertEqual(classify_code_language(css_sample), "css")
        self.assertEqual(classify_code_language(html_sample), "html")
        self.assertEqual(classify_code_language(html_snippet), "html")
        self.assertEqual(classify_code_language(js_sample), "javascript")
        self.assertEqual(classify_code_language("x = 10\nprint(x)"), "unknown")

    def test_split_multi_editor_code_fenced_blocks(self):
        fenced_code = (
            "```html\n<div class=\"container\">\n  <h1>Header</h1>\n</div>\n```\n\n"
            "```css\n.container {\n  padding: 20px;\n  background: #fff;\n}\n```"
        )
        parts = split_multi_editor_code(fenced_code)
        self.assertEqual(set(parts.keys()), {"html", "css"})
        self.assertIn("<h1>Header</h1>", parts["html"])
        self.assertIn("padding: 20px;", parts["css"])

    def test_split_multi_editor_code_comments(self):
        commented_code = (
            "<!-- HTML -->\n<div class=\"box\">Content</div>\n\n"
            "/* CSS */\n.box { border-radius: 8px; margin: 10px; }\n"
        )
        parts = split_multi_editor_code(commented_code)
        self.assertEqual(set(parts.keys()), {"html", "css"})
        self.assertEqual(parts["html"], "<div class=\"box\">Content</div>")
        self.assertEqual(parts["css"], ".box { border-radius: 8px; margin: 10px; }")

    def test_split_multi_editor_code_embedded_style_extraction(self):
        html_with_style = (
            "<!DOCTYPE html>\n<html>\n<head>\n"
            "<style>\nbody { margin: 0; }\n.btn { color: white; background: blue; }\n</style>\n"
            "</head>\n<body>\n<button class=\"btn\">Submit</button>\n</body>\n</html>"
        )
        parts = split_multi_editor_code(html_with_style)
        self.assertIn("html", parts)
        self.assertIn("css", parts)
        self.assertNotIn("<style>", parts["html"])
        self.assertIn(".btn { color: white; background: blue; }", parts["css"])
        self.assertIn("<button class=\"btn\">Submit</button>", parts["html"])

    def test_safe_write_auto_routes_css_to_css_target(self):
        page = MagicMock()
        written = {}

        async def mock_evaluate(script, *args):
            if "(code, target) =>" in script or "(code) =>" in script:
                if len(args) >= 2 and args[1]:
                    written[args[1]] = args[0]
                else:
                    written["default"] = args[0] if args else ""
                return True
            if "(target) =>" in script:
                target = args[0] if (args and args[0]) else "default"
                return written.get(target, "")
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            css_code = "body {\n  margin: 0;\n  padding: 0;\n  background: #333;\n}"
            success, snap, readback = loop.run_until_complete(
                safe_write(page, css_code, adapter=adapter)
            )
            self.assertTrue(success)
            self.assertIn("css", written)
            self.assertEqual(written["css"], css_code)
        finally:
            loop.close()

    def test_safe_write_auto_routes_html_to_html_target(self):
        page = MagicMock()
        written = {}

        async def mock_evaluate(script, *args):
            if "(code, target) =>" in script or "(code) =>" in script:
                if len(args) >= 2 and args[1]:
                    written[args[1]] = args[0]
                else:
                    written["default"] = args[0] if args else ""
                return True
            if "(target) =>" in script:
                target = args[0] if (args and args[0]) else "default"
                return written.get(target, "")
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            html_code = "<div class=\"box\">\n  <p>Hello World</p>\n</div>"
            success, snap, readback = loop.run_until_complete(
                safe_write(page, html_code, adapter=adapter)
            )
            self.assertTrue(success)
            self.assertIn("html", written)
            self.assertEqual(written["html"], html_code)
        finally:
            loop.close()

    def test_safe_write_splits_and_writes_both_html_and_css(self):
        page = MagicMock()
        written = {}

        async def mock_evaluate(script, *args):
            if "(code, target) =>" in script or "(code) =>" in script:
                if len(args) >= 2 and args[1]:
                    written[args[1]] = args[0]
                else:
                    written["default"] = args[0] if args else ""
                return True
            if "(target) =>" in script:
                target = args[0] if (args and args[0]) else "default"
                return written.get(target, "")
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            combined = (
                "```html\n<div class=\"card\">Card</div>\n```\n\n"
                "```css\n.card { border: 1px solid #ccc; }\n```"
            )
            success, snap, readback = loop.run_until_complete(
                safe_write(page, combined, adapter=adapter)
            )
            self.assertTrue(success)
            self.assertIn("html", written)
            self.assertIn("css", written)
            self.assertEqual(written["html"], "<div class=\"card\">Card</div>")
            self.assertEqual(written["css"], ".card { border: 1px solid #ccc; }")
        finally:
            loop.close()

    def test_get_all_editors_summary(self):
        page = MagicMock()

        async def mock_evaluate(script, *args):
            return [
                {
                    "index": 0,
                    "kind": "monaco",
                    "language": "html",
                    "label": "index.html",
                    "target_key": "html",
                    "content": "<!DOCTYPE html><html><body></body></html>",
                    "is_active": True,
                },
                {
                    "index": 1,
                    "kind": "monaco",
                    "language": "css",
                    "label": "style.css",
                    "target_key": "css",
                    "content": "/* write your css here */",
                    "is_active": False,
                },
            ]

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            editors = loop.run_until_complete(get_all_editors_summary(page))
            self.assertEqual(len(editors), 2)
            self.assertEqual(editors[0]["target_key"], "html")
            self.assertEqual(editors[1]["target_key"], "css")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
