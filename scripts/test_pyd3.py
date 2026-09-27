import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pydantic import BaseModel
class A(BaseModel):
    action: list
try:
    A.model_validate_json('not json')
except Exception as e:
    print(e.errors())
