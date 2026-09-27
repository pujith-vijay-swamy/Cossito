"""
find_timeout_in_browser_use.py
"""

import os, sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import browser_use
bu_dir = os.path.dirname(browser_use.__file__)
print(f"Scanning {bu_dir}...")

for root, _, files in os.walk(bu_dir):
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(root, f)
            lines = open(path, encoding="utf-8", errors="ignore").readlines()
            for i, line in enumerate(lines):
                if "timed out after" in line or "dead CDP" in line or "Action" in line and "timeout" in line:
                    rel_path = os.path.relpath(path, bu_dir)
                    print(f"{rel_path}:{i+1} -> {line.strip()}")
