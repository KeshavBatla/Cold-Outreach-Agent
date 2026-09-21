"""
Tavily web search tool integration.
"""

from typing import List, Dict, Any
from langchain_community.tools.tavily_search import TavilySearchResults
import config


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

    tool = get_tavily_tool(max_results=max_results)
    try:
        results = tool.invoke({"query": query.strip()})
        if isinstance(results, list):
            combined = "\n\n".join(r.get("content", "") for r in results if isinstance(r, dict))
            return f"--- Tavily Search Results ---\n{combined}"
        return f"--- Tavily Search Results ---\n{results}"
    except Exception as e:
        return f"--- Tavily Search Results ---\nError executing Tavily search: {e}"

