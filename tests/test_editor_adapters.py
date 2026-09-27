"""
Unit tests for editor_adapters.py.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from editor_adapters import (
    AceAdapter,
    CodeMirror5Adapter,
    CodeMirror6Adapter,
    MonacoAdapter,
    TextareaAdapter,
    detect_editor,
    safe_write,
)
from models import WorkspaceSnapshot


class TestEditorAdapters(unittest.TestCase):
    def test_monaco_adapter_snapshot_and_write(self):
        page = MagicMock()

        async def mock_evaluate(script, *args):
            # Enforce arrow function requirement like browser-use does
            self.assertTrue(script.strip().startswith("("), f"Script must start with '(': {script[:40]}")
            self.assertIn("=>", script, "Script must contain '=>'")
            if "models[0].setValue" in script:
                return True
            if "getLanguageId" in script:
                return "python"
            if "uri.toString" in script:
                return "inmemory://model/1"
            if "models[0].getValue()" in script:
                return "def old_code(): pass"
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)

        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            is_detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(is_detected)

            snap = loop.run_until_complete(adapter.snapshot(page))
            self.assertEqual(snap.editor_kind, "monaco")
            self.assertEqual(snap.content, "def old_code(): pass")
            self.assertEqual(snap.language, "python")
            self.assertEqual(snap.model_id, "inmemory://model/1")
            self.assertTrue(len(snap.content_hash) > 0)

            wrote = loop.run_until_complete(adapter.write(page, "def new_code(): return 1"))
            self.assertTrue(wrote)
        finally:
            loop.close()

    def test_safe_write_cycle(self):
        page = MagicMock()
        editor_content = ["initial content"]

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            if "models[0].setValue" in script:
                editor_content[0] = args[0]
                return True
            if "models[0].getValue()" in script:
                return editor_content[0]
            if "getLanguageId" in script:
                return "javascript"
            if "uri.toString" in script:
                return "inmemory://model/2"
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)

        loop = asyncio.new_event_loop()
        try:
            adapter = MonacoAdapter()
            success, snap_before, readback = loop.run_until_complete(
                safe_write(page, "function solve() { return 42; }", adapter)
            )
            self.assertTrue(success)
            self.assertEqual(snap_before.content, "initial content")
            self.assertEqual(readback, "function solve() { return 42; }")
        finally:
            loop.close()

    def test_codemirror5_adapter(self):
        page = MagicMock()
        cm5_content = ["def solve(): pass"]

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            if "el.CodeMirror.setValue" in script:
                cm5_content[0] = args[0]
                return True
            if "el.CodeMirror.getValue()" in script:
                return cm5_content[0]
            if "el.CodeMirror.getOption('mode')" in script:
                return "python"
            if ".CodeMirror" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = CodeMirror5Adapter()
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)

            snap = loop.run_until_complete(adapter.snapshot(page))
            self.assertEqual(snap.editor_kind, "codemirror5")
            self.assertEqual(snap.content, "def solve(): pass")
            self.assertEqual(snap.language, "python")

            wrote = loop.run_until_complete(adapter.write(page, "def solve(): return 100"))
            self.assertTrue(wrote)
            self.assertEqual(cm5_content[0], "def solve(): return 100")
        finally:
            loop.close()

    def test_codemirror6_adapter(self):
        page = MagicMock()
        cm6_content = ["let x = 1;"]

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            if "changes: { from: 0" in script or "el.textContent = code" in script:
                cm6_content[0] = args[0]
                return True
            if "view.state.doc.toString()" in script or "el.textContent" in script:
                return cm6_content[0]
            if ".cm-content" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = CodeMirror6Adapter()
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)

            snap = loop.run_until_complete(adapter.snapshot(page))
            self.assertEqual(snap.editor_kind, "codemirror6")

            wrote = loop.run_until_complete(adapter.write(page, "let x = 2;"))
            self.assertTrue(wrote)
            self.assertEqual(cm6_content[0], "let x = 2;")
        finally:
            loop.close()

    def test_ace_adapter(self):
        page = MagicMock()
        ace_content = ["// ace"]

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            if "win.ace.edit(el).setValue" in script:
                ace_content[0] = args[0]
                return True
            if "win.ace.edit(el).getValue()" in script:
                return ace_content[0]
            if "session.getMode().$id" in script:
                return "ace/mode/javascript"
            if ".ace_editor" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = AceAdapter()
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)

            snap = loop.run_until_complete(adapter.snapshot(page))
            self.assertEqual(snap.editor_kind, "ace")
            self.assertEqual(snap.language, "ace/mode/javascript")

            wrote = loop.run_until_complete(adapter.write(page, "// updated ace"))
            self.assertTrue(wrote)
            self.assertEqual(ace_content[0], "// updated ace")
        finally:
            loop.close()

    def test_textarea_adapter(self):
        page = MagicMock()
        ta_content = ["text"]

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            if "el.value = code" in script:
                ta_content[0] = args[0]
                return True
            if "return el.value;" in script:
                return ta_content[0]
            if "textarea" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = TextareaAdapter()
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)

            snap = loop.run_until_complete(adapter.snapshot(page))
            self.assertEqual(snap.editor_kind, "textarea")

            wrote = loop.run_until_complete(adapter.write(page, "new text"))
            self.assertTrue(wrote)
            self.assertEqual(ta_content[0], "new text")
        finally:
            loop.close()

    def test_safe_write_newline_normalization(self):
        page = MagicMock()

        # Simulate Windows CRLF vs Linux LF normalization
        async def mock_evaluate(script, *args):
            if "models[0].setValue" in script:
                return True
            if "models[0].getValue()" in script:
                # Editor normalized \r\n to \n
                return "line 1\nline 2\n"
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            adapter = MonacoAdapter()
            # Pass CRLF code
            success, snap, readback = loop.run_until_complete(
                safe_write(page, "line 1\r\nline 2", adapter)
            )
            self.assertTrue(success)
        finally:
            loop.close()

    def test_detect_editor_priority(self):
        page = MagicMock()

        async def mock_evaluate(script, *args):
            # Only CodeMirror 5 is present
            if "typeof win.monaco !== 'undefined'" in script:
                return False
            if ".CodeMirror" in script:
                return True
            return False

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            adapter = loop.run_until_complete(detect_editor(page))
            self.assertIsInstance(adapter, CodeMirror5Adapter)
        finally:
            loop.close()

    def test_monaco_bundled_dom_fallback(self):
        page = MagicMock()

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            # Bundled Monaco: window.monaco is undefined, but .monaco-editor is in DOM
            if "textarea.inputarea" in script or "pasteEv" in script:
                return True
            if "view-lines" in script:
                return "console.log('bundled monaco');"
            if ".monaco-editor" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)

            wrote = loop.run_until_complete(adapter.write(page, "console.log('bundled monaco');"))
            self.assertTrue(wrote)

            content = loop.run_until_complete(adapter.readback(page))
            self.assertEqual(content, "console.log('bundled monaco');")
        finally:
            loop.close()

    def test_safe_write_failover_to_next_adapter(self):
        page = MagicMock()

        # Primary Monaco fails write, but CodeMirror 5 succeeds
        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            # Monaco write returns False (e.g. read-only or detached)
            if "models[0].setValue" in script:
                return False
            if "models[0].getValue()" in script:
                return ""
            # CodeMirror 5 write returns True
            if "el.CodeMirror.setValue" in script:
                return True
            if "el.CodeMirror.getValue()" in script:
                return "def cm5_solution(): return 42"
            # Both Monaco and CM5 detected
            if ".monaco-editor" in script:
                return True
            if ".CodeMirror" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            # Safe write with no specific adapter will detect Monaco first, fail, and fail over to CM5
            success, snap, readback = loop.run_until_complete(
                safe_write(page, "def cm5_solution(): return 42")
            )
            self.assertTrue(success)
            self.assertEqual(readback, "def cm5_solution(): return 42")
        finally:
            loop.close()

    def test_safe_write_rejects_tiny_substring_readback(self):
        page = MagicMock()

        async def mock_evaluate(script, *args):
            # Editor returns write True, but readback is only tiny 15-char template
            if "models[0].setValue" in script:
                return True
            if "models[0].getValue()" in script:
                return "class Solution:"
            if "typeof win.monaco !== 'undefined'" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        adapter = MonacoAdapter()
        loop = asyncio.new_event_loop()
        try:
            # Code is a full 150-char solution; readback is just "class Solution:"
            full_code = "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        seen = {}\n        for i, n in enumerate(nums): return [0, 1]"
            success, snap, readback = loop.run_until_complete(
                safe_write(page, full_code, adapter)
            )
            # Must NOT accept tiny substring readback as verified!
            self.assertFalse(success)
        finally:
            loop.close()

    def test_textarea_adapter_filters_editor_internals(self):
        adapter = TextareaAdapter()
        page = MagicMock()

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            # Verify script filters out elements inside specialized editors
            self.assertIn(".closest('.monaco-editor, .CodeMirror, .ace_editor, .cm-editor", script)
            if "el.value = code" in script:
                return True
            if "return el.value;" in script:
                return "standalone textarea code"
            if "textarea" in script:
                return True
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertTrue(detected)
            wrote = loop.run_until_complete(adapter.write(page, "standalone textarea code"))
            self.assertTrue(wrote)
            readback = loop.run_until_complete(adapter.readback(page))
            self.assertEqual(readback, "standalone textarea code")
        finally:
            loop.close()

    def test_textarea_adapter_ignores_monaco_internal_inputarea(self):
        adapter = TextareaAdapter()
        page = MagicMock()

        async def mock_evaluate(script, *args):
            self.assertTrue(script.strip().startswith("("))
            self.assertIn("=>", script)
            # When textareas are filtered, none remain outside .monaco-editor
            return False

        page.evaluate = AsyncMock(side_effect=mock_evaluate)
        loop = asyncio.new_event_loop()
        try:
            detected = loop.run_until_complete(adapter.detect(page))
            self.assertFalse(detected)
            wrote = loop.run_until_complete(adapter.write(page, "code"))
            self.assertFalse(wrote)
            readback = loop.run_until_complete(adapter.readback(page))
            self.assertEqual(readback, "")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
