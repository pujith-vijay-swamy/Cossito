import re
with open(r'C:\Users\pujit\AppData\Local\Programs\Python\Python313\Lib\site-packages\browser_use\agent\service.py', encoding='utf-8') as f:
    text = f.read()

# Let's search for how llm is used
matches = re.findall(r'.{0,50}self\.llm.{0,50}', text)
print('\n'.join(matches[:20]))
