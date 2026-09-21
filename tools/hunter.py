"""
Hunter.io integration for company domain search, contact discovery, scoring, and verification.
"""

from typing import List, Dict, Any, Optional
import requests
import config
from utils.helpers import EMAIL_REGEX


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

    if any(x in position for x in high_priority):
        score += 100
    elif any(x in position for x in recruiter_titles):
        score += 80
    elif any(x in position for x in medium_priority):
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
    key = api_key or config.HUNTER_API_KEY
    if not company or not key:
        return []

    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"company": company, "api_key": key, "limit": 10},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[huntersearch] HTTP {resp.status_code} — {resp.text}")
            return []

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
        print(f"[huntersearch] Exception searching contacts: {e}")
        return []


def verify_email(email: str, api_key: Optional[str] = None) -> bool:
    """
    Verify an email address via Hunter.io email-verifier API.
    Returns True if status is 'valid' or 'accept_all'.
    """
    if not email or not EMAIL_REGEX.match(email):
        return False

    key = api_key or config.HUNTER_API_KEY
    if not key:
        print("[verify_email] HUNTER_API_KEY not configured, falling back to regex validation.")
        return True

    try:
        resp = requests.get(
            "https://api.hunter.io/v2/email-verifier",
            params={"email": email, "api_key": key},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        status = resp.json().get("data", {}).get("status")
        return status in ("valid", "accept_all")
    except Exception as e:
        print(f"[verify_email] Error during verification: {e}")
        return False

