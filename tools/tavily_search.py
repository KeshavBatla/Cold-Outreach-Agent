"""
Tavily web search tool integration.
"""

import random
import time
from typing import List, Dict, Any
from langchain_community.tools.tavily_search import TavilySearchResults
import requests
import config
from mocking import research_fixture


def get_tavily_tool(max_results: int = 5) -> TavilySearchResults:
    """
    Instantiate and return the Tavily search tool.
    """
    return TavilySearchResults(max_results=max_results)


def search_tavily(query: str, max_results: int = 5) -> str:
    """
    Execute a web search using Tavily and return concatenated findings.
    """
    if not query or not query.strip():
        return "Tavily Search: No search query provided."
    if config.MOCK_MODE:
        fixture = research_fixture(None)
        if fixture.get("outcome") == "failed":
            raise RuntimeError(fixture["error"])
        return "MOCK_FIXTURE: deterministic company and role research"

    tool = get_tavily_tool(max_results=max_results)
    retry_count = 0
    max_retries = max(0, min(config.MAX_RETRIES, 2))
    while True:
        try:
            results = tool.invoke({"query": query.strip()})
            if isinstance(results, list):
                combined = "\n\n".join(r.get("content", "") for r in results if isinstance(r, dict))
                return f"--- Tavily Search Results ---\n{combined}"
            return f"--- Tavily Search Results ---\n{results}"
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            retryable = (
                isinstance(error, (requests.Timeout, requests.ConnectionError))
                or status in {429, 500, 502, 503, 504}
                or any(token in type(error).__name__.casefold() for token in ("timeout", "serviceunavailable", "resourceexhausted"))
            )
            if not retryable or retry_count >= max_retries:
                raise RuntimeError(f"Tavily search failed: {error}") from error
            time.sleep(0.2 * (2 ** retry_count) + random.uniform(0, 0.15))
            retry_count += 1
