from pydantic import BaseModel
class A(BaseModel):
    action: list
try:
    A.model_validate({'items': []})
except Exception as e:
    import json
    # browser-use extracts error strings from validation errors like this:
    if hasattr(e, 'errors'):
        errors = e.errors()
        print([err['loc'] for err in errors])
