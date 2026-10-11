import os
import sys
import asyncio 

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.llm.model_client import get_llm

async def test_openai():
    llm = get_llm()
    result = await llm.ainvoke("你好")
    print(result.content)

asyncio.run(test_openai())