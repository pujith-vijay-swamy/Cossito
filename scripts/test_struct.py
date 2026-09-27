import os
from pydantic import BaseModel, Field
from llm_provider import get_llm

class TestOutput(BaseModel):
    items: list[str] = Field(description="A list of items.")

llm = get_llm(model="openai/gpt-4o-mini", api_key=os.environ.get("OPENROUTER_API_KEY"))
structured_llm = llm.with_structured_output(TestOutput)

try:
    result = structured_llm.invoke("Give me 3 colors.")
    print("SUCCESS!")
    print(repr(result))
except Exception as e:
    print("FAILED:", e)
    import traceback
    traceback.print_exc()
