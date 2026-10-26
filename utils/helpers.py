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


def strip_placeholder_links(text: str, candidate_name: Optional[str] = None) -> str:
    """
    Remove placeholder links and labels (e.g. 'GitHub: [Link]', 'LinkedIn: [Link]')
    while keeping real links intact. Replaces candidate name placeholders if candidate_name is known.
    """
    if not text:
        return ""

    # Remove lines that are purely empty placeholder references
    cleaned = re.sub(
        r"(?im)^[ \t]*(?:GitHub|LinkedIn|Portfolio|Website|Resume)\s*:\s*\[?(?:Link|URL|None|TBD)\]?[ \t]*\r?\n?",
        "",
        text,
    )
    # Remove inline empty placeholder tokens like ' [Link]' or ' (Link)'
    cleaned = re.sub(r"(?i)\b(?:GitHub|LinkedIn|Portfolio|Website)\s*:\s*\[?(?:Link|URL)\]?", "", cleaned)

    if candidate_name and candidate_name.strip():
        cleaned = re.sub(
            r"(?i)\[(?:Your Name|Candidate Name|Name)\]",
            candidate_name.strip(),
            cleaned,
        )

    return cleaned.strip()


def find_unresolved_placeholders(text: str) -> list[str]:
    """
    Detect unresolved bracketed placeholder tokens in text, e.g. [Link], [URL], [Your Name], etc.
    """
    if not text:
        return []
    pattern = re.compile(
        r"\[(?i:Link|URL|Your Name|Candidate Name|Company Name|Insert [^\]]+|Date|Phone|Email|Website|Portfolio)\]"
    )
    matches = pattern.findall(text)
    # Also check for bracketed text containing '[your ...' or '[name]'
    extra_pattern = re.compile(r"\[(?i:your\s+[^\]]+|name)\]")
    for m in extra_pattern.findall(text):
        if m not in matches:
            matches.append(m)
    return matches
