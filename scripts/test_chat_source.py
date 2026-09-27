import browser_use.llm.browser_use.chat as c
import inspect
source = inspect.getsource(c)
print(source.encode('ascii', 'replace').decode('ascii'))
