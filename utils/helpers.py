"""
Helper utilities for text extraction, validation, and deduplication.
"""

import re
from typing import Any, Set

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PLACEHOLDER_VALUES = {
    "not specified",
    "n/a",
    "na",
    "unknown",
    "none",
    "tbd",
    "unspecified",
    "not available",
}

# In-memory registry of already contacted companies (can be populated or extended)
CONTACTED_COMPANIES: Set[str] = set()


def extract_text(content: Any) -> str:
    """
    Safely extract plain text from diverse LangChain message content types
    (strings, lists of content dicts, objects).
    """
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content).strip() if content is not None else ""


def is_already_mailed(company_name: str, contacted_set: Set[str] = None) -> bool:
    """
    Check if a company has already been targeted/contacted to prevent spam or duplicate outreach.
    """
    if not company_name:
        return False
    target = company_name.strip().lower()
    registry = contacted_set if contacted_set is not None else CONTACTED_COMPANIES
    return any(target in c or c in target for c in registry)


def register_contacted_company(company_name: str) -> None:
    """
    Add a company name to the in-memory contacted registry.
    """
    if company_name:
        CONTACTED_COMPANIES.add(company_name.strip().lower())

