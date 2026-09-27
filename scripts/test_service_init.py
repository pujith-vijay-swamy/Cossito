with open(r'C:\Users\pujit\AppData\Local\Programs\Python\Python313\Lib\site-packages\browser_use\agent\service.py', encoding='utf-8') as f:
    text = f.read()
print('\n'.join(text.split('\n')[200:230]).encode('ascii', 'replace').decode('ascii'))
