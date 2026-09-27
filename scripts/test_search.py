import re
with open(r'C:\Users\pujit\AppData\Local\Programs\Python\Python313\Lib\site-packages\browser_use\agent\service.py', encoding='utf-8') as f:
    text = f.read()
matches = re.findall(r'.{0,30}items.{0,30}', text)
print('\n'.join(matches))
