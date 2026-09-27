import re
with open(r'C:\Users\pujit\AppData\Local\Programs\Python\Python313\Lib\site-packages\browser_use\browser\session.py', encoding='utf-8') as f:
    text = f.read()
print(re.findall(r'.{0,50}keep_alive.{0,50}', text))
