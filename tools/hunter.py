"""
Hunter.io integration for company domain search, contact discovery, scoring, and verification.
"""

import os
import re
import random
import time
import warnings
from typing import List, Dict, Any, Optional
import requests
import config
from mocking import contact_fixtures
from utils.helpers import EMAIL_REGEX


def _matches_phrase(phrases: List[str], text: str) -> bool:
    """Helper to check if any phrase matches in text using whole-phrase word boundaries."""
    return any(bool(re.search(rf"\b{re.escape(p)}\b", text)) for p in phrases)


def score_contact(e: Dict[str, Any]) -> int:
    """
    Score and prioritize contacts based on department, job title, seniority, and email type.
    Prioritizes Engineering Managers, Tech Leads, and Technical Recruiters.
    """
    position = (e.get("position") or "").lower()
    dept = (e.get("department") or "").lower()
    score = 0

    # Boost HR / IT departments
    if dept in ["it", "hr"]:
        score += 15

    # Priority 1: Hiring Managers / Tech Leadership
    high_priority = [
        "engineering manager",
        "software engineering manager",
        "director of engineering",
        "head of engineering",
        "vp engineering",
        "vice president engineering",
        "head of ai",
        "head of machine learning",
        "ml engineering manager",
        "ai engineering manager",
        "cto",
    ]

    # Priority 2: Talent Acquisition / Recruiters
    recruiter_titles = [
        "technical recruiter",
        "recruiter",
        "talent acquisition",
        "recruiting manager",
        "head of talent",
    ]

    # Priority 3: Tech Leads / Senior Engineers
    medium_priority = [
        "engineering lead",
        "technical lead",
        "engineering director",
        "software engineering",
        "machine learning",
        "artificial intelligence",
    ]

    if _matches_phrase(high_priority, position):
        score += 100
    elif _matches_phrase(recruiter_titles, position):
        score += 80
    elif _matches_phrase(medium_priority, position):
        score += 50

    # Seniority boost
    seniority = (e.get("seniority") or "").lower()
    if seniority == "executive":
        score += 30
    elif seniority == "senior":
        score += 20

    # Email type preference (personal vs generic)
    if e.get("type") == "personal":
        score += 10

    return score


def search_domain_contacts(company: str, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Query Hunter.io domain-search endpoint for contacts matching a company name.
    Returns scored and sorted candidates list: [{"name": ..., "email": ..., "position": ...}].
    """
    if config.MOCK_MODE:
        return contact_fixtures()
    key = api_key or config.HUNTER_API_KEY
    if not company or not key:
        raise ValueError("Company and HUNTER_API_KEY are required for Hunter domain search.")

    try:
        resp = _hunter_request(
            "https://api.hunter.io/v2/domain-search",
            {"company": company, "api_key": key, "limit": 10},
        )

        data = resp.json()
        contacts = data.get("data", {}).get("emails", [])
        print(
            f"[huntersearch] company={company!r} resolved to domain={data.get('data', {}).get('domain')!r}, "
            f"contacts found={len(contacts)}"
        )
        if not contacts:
            return []

        contacts.sort(key=score_contact, reverse=True)
        candidates = [
            {
                "name": f"{e.get('first_name', '')} {e.get('last_name', '')}".strip(),
                "email": e.get("value"),
                "position": e.get("position"),
            }
            for e in contacts
        ]
        return candidates
    except Exception as e:
        raise RuntimeError(f"Hunter domain search failed: {e}") from e


def verify_email(email: str, api_key: Optional[str] = None) -> bool:
    """
    Verify an email address via Hunter.io email-verifier API.
    Returns True if status is 'valid' or 'accept_all'.
    """
    if config.MOCK_MODE:
        return bool(email and EMAIL_REGEX.match(email) and config.MOCK_SCENARIO != "invalid_contact")
    if not email or not EMAIL_REGEX.match(email):
        return False

    key = api_key or config.HUNTER_API_KEY
    if not key:
        allow_unverified = os.getenv("ALLOW_UNVERIFIED_EMAILS", "").strip().lower() in ("true", "1", "yes")
        if allow_unverified:
            return True
        warnings.warn(
            "[verify_email] HUNTER_API_KEY is not set. Failing email verification. "
            "Set ALLOW_UNVERIFIED_EMAILS=true in your environment to bypass."
        )
        return False

    try:
        resp = _hunter_request(
            "https://api.hunter.io/v2/email-verifier",
            {"email": email, "api_key": key},
        )
        status = resp.json().get("data", {}).get("status")
        return status in ("valid", "accept_all")
    except Exception as e:
        raise RuntimeError(f"Hunter email verification failed: {e}") from e


def _hunter_request(url: str, params: Dict[str, Any]):
    """Retry only transient Hunter failures a bounded number of times."""
    attempts = max(0, min(config.MAX_RETRIES, 2))
    for attempt in range(attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=10)
        except requests.RequestException:
            if attempt >= attempts:
                raise
            time.sleep((0.2 * (2 ** attempt)) + random.uniform(0, 0.15))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt >= attempts:
                raise RuntimeError(f"Hunter returned transient HTTP {response.status_code}.")
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 5.0) if retry_after else 0.2 * (2 ** attempt)
            except ValueError:
                delay = 0.2 * (2 ** attempt)
            time.sleep(delay + random.uniform(0, 0.15))
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Hunter returned HTTP {response.status_code}: {response.text}")
        return response
    raise RuntimeError("Hunter request exhausted its retry budget.")
