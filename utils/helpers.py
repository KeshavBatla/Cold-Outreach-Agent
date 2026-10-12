"""
Helper utilities for text extraction, validation, and deduplication.
"""

import re
from typing import Any, Optional, Set

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


def is_already_mailed(company_name: str, contacted_set: Optional[Set[str]] = None) -> bool:
    """
    Check persistent successful-outreach history, or an explicitly supplied legacy test set.
    """
    if not company_name:
        return False
    target = company_name.strip().lower()
    if contacted_set is not None:
        return any(target == company.strip().lower() for company in contacted_set)
    from persistence import company_block_reason

    return company_block_reason(company_name, None) == "Recently contacted through this service."


def register_contacted_company(company_name: str) -> None:
    """
    Reject the old unsafe API, which could mark research as successful delivery.
    """
    raise RuntimeError(
        "Research is not a successful outreach event. Record delivery through the persistence repository."
    )
