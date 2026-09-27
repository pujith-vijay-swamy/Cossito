"""
inspect_action_timeout.py
"""

import sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import browser_use
import inspect

print("Agent parameters:")
sig = inspect.signature(browser_use.Agent.__init__)
for p in sig.parameters.values():
    print(f"  {p.name}: default={p.default}")

# Look for Controller or Tool execution timeout
from browser_use.controller.service import Controller
print("\nController methods and properties:")
print(dir(Controller))

# Search for where 'timed out after' or 'dead CDP WebSocket' is raised in browser_use
import os
bu_path = os.path.dirname(browser_use.__file__)
print(f"\nbrowser_use path: {bu_path}")

for root, dirs, files in os.walk(bu_path):
    for file in files:
        if file.endswith(".py"):
            fpath = os.path.join(root, file)
            content = open(fpath, encoding="utf-8", errors="ignore").read()
            if "timed out after" in content or "180" in content:
                print(f"Found match in {file}")
                for line in content.splitlines():
                    if "timed out after" in line or "180" in line or "timeout" in line.lower() and "action" in line.lower():
                        print(f"  {line.strip()[:100]}")
