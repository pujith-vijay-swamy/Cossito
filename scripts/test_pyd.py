from pydantic import BaseModel
class A(BaseModel):
    action: list
try:
    A.model_validate({'action': {}})
except Exception as e:
    print(repr(e))
