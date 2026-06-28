"""Quick test: verify LLM connectivity."""

import asyncio
from langchain_openai import ChatOpenAI
from src.config import settings


async def main():
    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        temperature=0.1,
        max_tokens=128,
    )
    print(f"Testing model: {settings.llm_model}")
    print(f"Base URL: {settings.llm_base_url}")
    try:
        resp = await llm.ainvoke('Reply with just "OK"')
        print(f"Response: {resp.content[:200]}")
        print("Connection: SUCCESS")
    except Exception as e:
        print(f"Connection: FAILED - {e}")


if __name__ == "__main__":
    asyncio.run(main())
