import re
import glob
import os

matches = []
for d in [r'C:\Users\pujit\AppData\Local\Programs\Python\Python313\Lib\site-packages\browser_use']:
    for root, dirs, files in os.walk(d):
        for file in files:
            if file.endswith('.py'):
                fpath = os.path.join(root, file)
                try:
                    content = open(fpath, encoding='utf-8', errors='ignore').read()
                    if 'output_format' in content:
                        matches.append(fpath)
                except Exception:
                    pass

print('\n'.join(matches))
