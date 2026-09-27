import re
with open('test_debug.log', encoding='utf-8', errors='ignore') as f:
    text = f.read()
print(text[-3000:])
