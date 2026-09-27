"""
inspect_tools_service.py
"""

import sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import browser_use
import os

bu_dir = os.path.dirname(browser_use.__file__)
path = os.path.join(bu_dir, "tools", "service.py")
lines = open(path, encoding="utf-8", errors="ignore").readlines()

start = max(0, 2160 - 1)
end = min(len(lines), 2210)

for i in range(start, end):
    print(f"{i+1}: {lines[i]}", end="")
