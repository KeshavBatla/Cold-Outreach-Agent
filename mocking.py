"""Deterministic integration fixtures used only when MOCK_MODE is enabled."""

from typing import Any

import config


def research_fixture(seed_company: str | None) -> dict[str, Any]:
    scenario = config.MOCK_SCENARIO
    company = seed_company or "Mock AI Labs"
    domain = "mock-ai.example"
    if scenario == "provider_timeout":
        return {"error": "Simulated provider timeout", "outcome": "failed"}
    if scenario == "role_mismatch":
        role = "Senior Sales Director"
    else:
        role = "AI Engineer Intern"
    contact_email = (
        None
        if scenario == "no_contacts"
        else "invalid-address"
        if scenario == "invalid_contact"
        else "alex@mock-ai.example"
    )
    return {
        "company_name": company,
        "company_domain": domain,
        "contact_name": "Alex Example",
        "contact_email": contact_email,
        "job_role": role,
        "job_description": f"{role} role focused on LLM applications and agentic systems.",
        "research_summary": f"{company} is hiring for a role in agentic AI and LLM applications.",
        "outcome": "candidate_found",
    }


def contact_fixtures() -> list[dict[str, str]]:
    if config.MOCK_SCENARIO == "no_contacts":
        return []
    if config.MOCK_SCENARIO == "role_mismatch":
        return []
    if config.MOCK_SCENARIO == "invalid_contact":
        return [{"name": "Alex Example", "email": "invalid-address", "position": "Technical Recruiter"}]
    return [{"name": "Alex Example", "email": "alex@mock-ai.example", "position": "AI Engineering Manager"}]
