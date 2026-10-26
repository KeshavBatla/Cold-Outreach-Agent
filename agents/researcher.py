"""
Researcher Agent module.
Generates targeted queries, searches via Tavily, extracts structured details,
ranks contacts via Hunter.io, evaluates role fit, and pauses for human verification.
"""

import re
import time
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config as app_config
from graph.state import ResearcherState, OutreachState
from prompts.researcher import (
    RESEARCHER_QUERY_SYSTEM_PROMPT,
    RESEARCHER_QUERY_HUMAN_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    COMPANY_NAME_FALLBACK_PROMPT,
    RESEARCH_EVALUATION_SYSTEM_PROMPT,
)
from evaluation.llm_evaluator import (
    ResearchExtraction,
    ResearchEvaluation,
    get_research_extractor,
    get_company_name_extractor,
    get_research_evaluator,
)
from tools.tavily_search import search_tavily
from tools.hunter import search_domain_contacts, verify_email
from utils.helpers import extract_text, PLACEHOLDER_VALUES, EMAIL_REGEX
from mocking import research_fixture, contact_fixtures
from persistence import (
    add_exclusion,
    company_block_reason,
    get_cached_contacts,
    get_cached_research,
    get_usage_summary,
    normalize_company_name,
    normalize_domain,
    record_provider_usage,
    record_research_run,
    research_cache_key,
    save_contacts,
    set_cached_research,
    update_contact_verification,
    ensure_external_call_allowed,
)
from utils.observability import invoke_with_usage


def inputpromptformater(state: ResearcherState) -> Dict[str, Any]:
    explicit_mode = state.get("mode")
    if explicit_mode in {"open_role", "speculative"}:
        mode = explicit_mode
    else:
        mode = "open_role" if state.get("seed_query") else "speculative"
    search_attempt = state.get("search_attempt", 0) + 1
    search_budget = state.get(
        "search_budget",
        state.get("max_retries", app_config.SPECULATIVE_SEARCH_BUDGET),
    )
    if app_config.MOCK_MODE:
        query = state.get("seed_query") or "Find one matching opportunity for the candidate profile"
        return {
            "search_query": query,
            "mode": mode,
            "search_attempt": search_attempt,
            "search_budget": search_budget,
        }
    llm = app_config.get_llm()
    profile = state.get("candidate_profile", "")
    seed_query = state.get("seed_query", "")
    seniority = state.get("target_seniority") or ""
    domain_focus = state.get("target_domain_keywords") or ""
    location_preference = state.get("location_preference") or ""

    system_text = RESEARCHER_QUERY_SYSTEM_PROMPT.format(
        profile=profile,
        mode=mode,
        seed_query=seed_query,
        seniority=seniority,
        domain_focus=domain_focus,
        location_preference=location_preference,
        search_attempt=search_attempt,
        search_budget=search_budget,
        rejected_candidates=state.get("rejected_candidates") or [],
    )
    response = invoke_with_usage(
        llm.invoke,
        [
            SystemMessage(content=system_text),
            HumanMessage(content=RESEARCHER_QUERY_HUMAN_PROMPT),
        ],
        provider="gemini",
        operation="research_query",
        model=app_config.MODEL_NAME,
        run_id=state.get("research_run_id") or "unknown",
    )
    query_text = extract_text(response.content)
    if mode == "speculative":
        rejected = [
            cand.get("company_name")
            for cand in state.get("rejected_candidates") or []
            if cand.get("company_name")
        ]
        for name in rejected[:8]:
            clean_name = re.sub(r"[^\w\s-]", "", name).strip()
            if clean_name and clean_name.lower() not in query_text.lower():
                query_text += f' -"{clean_name}"'
    return {
        "search_query": query_text,
        "mode": mode,
        "search_attempt": search_attempt,
        "search_budget": search_budget,
    }


def tavilysearch(state: ResearcherState) -> Dict[str, Any]:
    if app_config.MOCK_MODE:
        fixture = research_fixture(state.get("seed_query"))
        if fixture.get("outcome") == "failed":
            return {"research_error": fixture["error"], "research_outcome": "failed"}
        return {"tavily_findings": "MOCK_FIXTURE: deterministic company and role research"}
    query = extract_text(state.get("search_query", ""))
    started = time.monotonic()
    try:
        ensure_external_call_allowed(state.get("research_run_id") or "unknown")
        findings = search_tavily(query)
    except Exception as error:
        record_provider_usage(
            run_id=state.get("research_run_id") or "unknown",
            provider="tavily",
            operation="search",
            successful=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=str(error),
        )
        return {"research_error": str(error), "research_outcome": "failed"}
    record_provider_usage(
        run_id=state.get("research_run_id") or "unknown",
        provider="tavily",
        operation="search",
        successful=True,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"tavily_findings": findings, "research_error": None}


def formatingnode(state: ResearcherState) -> Dict[str, Any]:
    if app_config.MOCK_MODE:
        fixture = research_fixture(state.get("seed_query"))
        if fixture.get("outcome") == "failed":
            return {"research_outcome": "failed", "research_error": fixture["error"]}
        return {
            "company_name": fixture["company_name"],
            "company_domain": fixture["company_domain"],
            "contact_name": fixture["contact_name"],
            "contact_email": fixture["contact_email"],
            "job_role": fixture["job_role"],
            "job_description": fixture["job_description"],
            "research_summary": fixture["research_summary"],
        }
    llm = app_config.get_llm()
    merged = state.get("tavily_findings") or "No findings available."
    research_extractor = get_research_extractor(llm)

    rejected_candidates = state.get("rejected_candidates") or []
    rejected_names = [
        cand.get("company_name")
        for cand in rejected_candidates
        if cand.get("company_name")
    ]
    prompt_text = EXTRACTION_SYSTEM_PROMPT
    if rejected_names:
        prompt_text += (
            f"\nIMPORTANT EXCLUSIONS: Do NOT select any of the following companies, as they have already been evaluated: "
            f"{', '.join(rejected_names)}. If any of these are mentioned in the research text, select a DIFFERENT company mentioned in the text."
        )

    messages = [
        SystemMessage(content=prompt_text),
        HumanMessage(content=merged),
    ]
    result = invoke_with_usage(
        research_extractor.invoke,
        {"messages": messages},
        provider="gemini",
        operation="research_extraction",
        model=app_config.MODEL_NAME,
        run_id=state.get("research_run_id") or "unknown",
    )
    extracted: ResearchExtraction = result["responses"][0]
    company_name = extracted.company_name

    if company_name and company_name.strip().lower() in PLACEHOLDER_VALUES:
        company_name = None

    if company_name and any(
        normalize_company_name(company_name) == normalize_company_name(r)
        for r in rejected_names
    ):
        company_name_extractor = get_company_name_extractor(llm)
        fallback = invoke_with_usage(
            company_name_extractor.invoke,
            {
                "messages": [
                    SystemMessage(
                        content=f"Identify a company mentioned in the text other than: {', '.join(rejected_names)}. Return ONLY the new company name, or leave empty if no other company is mentioned."
                    ),
                    HumanMessage(content=merged),
                ]
            },
            provider="gemini",
            operation="company_name_extraction_alternative",
            model=app_config.MODEL_NAME,
            run_id=state.get("research_run_id") or "unknown",
        )
        alt_name = fallback["responses"][0].company_name
        if alt_name and not any(
            normalize_company_name(alt_name) == normalize_company_name(r)
            for r in rejected_names
        ):
            company_name = alt_name
        else:
            company_name = None

    if not company_name and extracted.research_summary:
        company_name_extractor = get_company_name_extractor(llm)
        fallback = invoke_with_usage(
            company_name_extractor.invoke,
            {
                "messages": [
                    SystemMessage(content=COMPANY_NAME_FALLBACK_PROMPT),
                    HumanMessage(content=extracted.research_summary),
                ]
            },
            provider="gemini",
            operation="company_name_extraction",
            model=app_config.MODEL_NAME,
            run_id=state.get("research_run_id") or "unknown",
        )
        fb_name = fallback["responses"][0].company_name
        if fb_name and not any(
            normalize_company_name(fb_name) == normalize_company_name(r)
            for r in rejected_names
        ):
            company_name = fb_name
        print(f"[formatingnode] company_name was blank, fallback found: {company_name!r}")

    urls = re.findall(r"https?://([^/\s)]+)", merged)
    company_domain = next((normalize_domain(url) for url in urls if normalize_domain(url)), None)
    if not company_domain and extracted.contact_email and EMAIL_REGEX.match(extracted.contact_email):
        company_domain = normalize_domain(extracted.contact_email.rsplit("@", 1)[-1])

    result = {
        "company_name": company_name,
        "company_domain": company_domain,
        "contact_name": extracted.contact_name,
        "contact_email": extracted.contact_email,
        "job_role": extracted.job_role,
        "job_description": extracted.job_description,
        "research_summary": extracted.research_summary,
    }
    cache_key = state.get("cache_key")
    if cache_key and state.get("mode") != "speculative" and state.get("seed_query"):
        public_cache_result = {
            key: value
            for key, value in result.items()
            if key not in {"contact_name", "contact_email"}
        }
        set_cached_research(cache_key, public_cache_result)
    return result


def huntersearch(state: ResearcherState) -> Dict[str, Any]:
    company = state.get("company_name")
    if not company:
        return {"hunter_candidates": [], "hunter_candidate_index": 0, "research_outcome": "no_matching_role"}

    block_reason = company_block_reason(
        company,
        state.get("company_domain"),
        owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
    )
    if block_reason:
        if state.get("mode") == "speculative":
            return {
                "hunter_candidates": [],
                "hunter_candidate_index": 0,
                "research_outcome": "no_usable_contact",
                "evaluator_feedback": f"Opportunity skipped by existing eligibility safeguards: {block_reason}",
            }
        return {
            "hunter_candidates": [],
            "hunter_candidate_index": 0,
            "research_outcome": "failed",
            "research_error": block_reason,
        }

    tavily_email = state.get("contact_email")
    tavily_candidate = None
    if tavily_email and EMAIL_REGEX.match(tavily_email):
        tavily_candidate = {
            "name": state.get("contact_name"),
            "email": tavily_email,
            "position": "from Tavily",
        }

    if tavily_candidate:
        candidates = [tavily_candidate]
    elif app_config.MOCK_MODE:
        candidates = contact_fixtures()
    else:
        candidates = get_cached_contacts(
            company,
            state.get("company_domain"),
            run_id=state.get("research_run_id") or "unknown",
            owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        )
        if not candidates:
            started = time.monotonic()
            try:
                ensure_external_call_allowed(state.get("research_run_id") or "unknown")
                candidates = search_domain_contacts(company)
            except Exception as error:
                record_provider_usage(
                    run_id=state.get("research_run_id") or "unknown",
                    provider="hunter",
                    operation="domain_search",
                    successful=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=str(error),
                )
                return {
                    "hunter_candidates": [],
                    "hunter_candidate_index": 0,
                    "research_outcome": "failed",
                    "research_error": str(error),
                }
            record_provider_usage(
                run_id=state.get("research_run_id") or "unknown",
                provider="hunter",
                operation="domain_search",
                successful=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            save_contacts(
                company,
                state.get("company_domain"),
                candidates,
                owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
            )
    candidates = candidates[:app_config.MAX_CONTACTS_PER_COMPANY]
    if not candidates:
        return {
            "hunter_candidates": [],
            "hunter_candidate_index": 0,
            "research_outcome": "no_usable_contact",
            "contact_attempts": 0,
        }

    return {
        "hunter_candidates": candidates[:app_config.MAX_CONTACTS_PER_COMPANY],
        "hunter_candidate_index": 0,
        "contact_attempts": 0,
    }


def trynextcandidate(state: ResearcherState) -> Dict[str, Any]:
    candidates = state.get("hunter_candidates") or []
    idx = state.get("hunter_candidate_index", 0)
    if idx >= len(candidates) or state.get("contact_attempts", 0) >= app_config.MAX_EMAIL_VERIFICATIONS:
        print(f"[trynextcandidate] Exhausted all {len(candidates)} candidates for this company.")
        return {
            "contact_name": None,
            "contact_email": None,
            "contact_verified": False,
            "research_outcome": "no_usable_contact",
        }

    pick = candidates[idx]
    print(f"[trynextcandidate] Trying candidate {idx + 1}/{len(candidates)}")
    return {
        "contact_name": pick["name"],
        "contact_email": pick["email"],
        "hunter_candidate_index": idx + 1,
        "contact_verified": None,
    }


def _candidate_key(company_name: str | None, job_role: str | None) -> tuple[str, str]:
    return (
        " ".join((company_name or "").casefold().split()),
        " ".join((job_role or "").casefold().split()),
    )


def _matches_named_target(state: ResearcherState) -> bool:
    target = (state.get("seed_query") or "").strip()
    if not target:
        return True
    if "." in target and " " not in target:
        target_domain = normalize_domain(target)
        return bool(
            target_domain
            and target_domain
            in {
                normalize_domain(state.get("company_domain")),
                normalize_domain(state.get("company_name")),
            }
        )
    return normalize_company_name(target) == normalize_company_name(state.get("company_name"))


def _record_rejected_candidate(state: ResearcherState) -> list[dict[str, str]]:
    company_name = state.get("company_name")
    job_role = state.get("job_role")
    evaluator_feedback = state.get("evaluator_feedback") or ""
    eligibility_reason = state.get("eligibility_reason") or evaluator_feedback

    if evaluator_feedback:
        reason = evaluator_feedback
    elif state.get("research_sufficient"):
        reason = "The role met the fit criteria, but no usable contact was found."
    else:
        reason = "Role did not meet the research criteria."

    candidate = {
        "status": "rejected",
        "company_name": company_name or "",
        "company_domain": state.get("company_domain") or "",
        "job_role": job_role or "",
        "reason": reason,
    }
    rejected = list(state.get("rejected_candidates") or [])
    key = _candidate_key(company_name, job_role)
    if any(
        _candidate_key(entry.get("company_name"), entry.get("job_role")) == key
        for entry in rejected
    ):
        return rejected
    if company_name or job_role:
        rejected.append(candidate)
    return rejected


def llm_evaluates(state: ResearcherState) -> Dict[str, Any]:
    if (
        state.get("mode") == "open_role"
        and state.get("seed_query")
        and not _matches_named_target(state)
    ):
        return {
            "research_sufficient": False,
            "research_outcome": "no_matching_role",
            "evaluator_feedback": "Research did not match the specified company or domain.",
            "eligibility_reason": "Research did not match the specified company or domain.",
        }
    company_name = state.get("company_name")
    rejected_list = state.get("rejected_candidates") or []
    key = _candidate_key(company_name, state.get("job_role"))
    is_role_rejected = any(
        _candidate_key(entry.get("company_name"), entry.get("job_role")) == key
        for entry in rejected_list
    )
    is_company_excluded = (
        state.get("mode") == "speculative"
        and company_name
        and any(
            normalize_company_name(company_name) == normalize_company_name(entry.get("company_name"))
            for entry in rejected_list
            if entry.get("company_name") and (
                not entry.get("job_role")
                or "excluded" in (entry.get("reason") or "").lower()
                or "user" in (entry.get("reason") or "").lower()
                or "sibling" in (entry.get("reason") or "").lower()
                or "already targeted" in (entry.get("reason") or "").lower()
            )
        )
    )
    if is_role_rejected or is_company_excluded:
        reason_msg = "This company was already evaluated; searching for a different opportunity."
        return {
            "research_sufficient": False,
            "research_outcome": "no_matching_role",
            "evaluator_feedback": reason_msg,
            "eligibility_reason": reason_msg,
            "retry_count": state.get("retry_count", 0) + 1,
            "retry_counted": True,
        }
    if app_config.MOCK_MODE:
        suitable = app_config.MOCK_SCENARIO != "role_mismatch"
        return {
            "research_sufficient": suitable,
            "research_outcome": "candidate_found" if suitable else "no_matching_role",
            "evaluator_feedback": "Mock role fit accepted." if suitable else "Mock role does not match the requested profile.",
            "eligibility_reason": "Mock role fit accepted." if suitable else "Mock role does not match the requested profile.",
            "retry_count": state.get("retry_count", 0) + (0 if suitable else 1),
            "retry_counted": not suitable,
        }
    if not state.get("company_name") or not state.get("job_role"):
        return {
            "research_sufficient": False,
            "research_outcome": "no_matching_role",
            "evaluator_feedback": "No company or suitable role was identified.",
            "eligibility_reason": "No company or suitable role was identified.",
            "retry_count": state.get("retry_count", 0) + 1,
            "retry_counted": True,
        }

    llm = app_config.get_llm()
    research_evaluator = get_research_evaluator(llm)

    target_seniority = state.get("target_seniority") or "fresher / junior / intern"
    target_keywords = state.get("target_domain_keywords") or "AI / software / tech"
    candidate_profile = state.get("candidate_profile") or "Early-career software / AI engineer"

    messages = [
        SystemMessage(content=RESEARCH_EVALUATION_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Candidate profile: {candidate_profile}\n"
                f"Requested seniority: {target_seniority}\n"
                f"Target domain keywords: {target_keywords}\n"
                f"Company name: {state.get('company_name')}\n"
                f"Job role: {state.get('job_role')}\n"
                f"Job description: {state.get('job_description') or 'Not provided'}\n"
                f"Research summary: {state.get('research_summary')}"
            )
        ),
    ]
    result = invoke_with_usage(
        research_evaluator.invoke,
        {"messages": messages},
        provider="gemini",
        operation="role_evaluation",
        model=app_config.MODEL_NAME,
        run_id=state.get("research_run_id") or "unknown",
    )
    evaluation: ResearchEvaluation = result["responses"][0]
    reason = evaluation.reason or evaluation.feedback
    updates = {
        "evaluator_feedback": evaluation.feedback,
        "eligibility_reason": reason,
        "company_relevant": evaluation.company_relevant,
        "role_suitable": evaluation.role_suitable,
        "seniority_match": evaluation.seniority_match,
        "experience_required": evaluation.experience_required,
        "research_sufficient": evaluation.is_sufficient,
        "research_outcome": "candidate_found" if evaluation.is_sufficient else "no_matching_role",
        "retry_counted": not evaluation.is_sufficient,
    }
    if not evaluation.is_sufficient:
        updates["retry_count"] = state.get("retry_count", 0) + 1
    return updates


def verify_candidate(state: ResearcherState) -> Dict[str, Any]:
    email = state.get("contact_email")
    cached_status = next(
        (
            candidate.get("verification_status")
            for candidate in state.get("hunter_candidates") or []
            if candidate.get("email", "").casefold() == (email or "").casefold()
        ),
        None,
    )
    if cached_status == "valid":
        valid = True
    elif cached_status == "invalid":
        valid = False
    elif not email or not EMAIL_REGEX.match(email):
        valid = False
    elif app_config.MOCK_MODE:
        valid = app_config.MOCK_SCENARIO not in {"invalid_contact"}
    else:
        started = time.monotonic()
        try:
            ensure_external_call_allowed(state.get("research_run_id") or "unknown")
            valid = verify_email(email)
        except Exception as error:
            record_provider_usage(
                run_id=state.get("research_run_id") or "unknown",
                provider="hunter",
                operation="verify_email",
                successful=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=str(error),
            )
            return {
                "contact_verified": False,
                "research_outcome": "failed",
                "research_error": str(error),
            }
        record_provider_usage(
            run_id=state.get("research_run_id") or "unknown",
            provider="hunter",
            operation="verify_email",
            successful=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        update_contact_verification(
            state.get("company_name") or "",
            state.get("company_domain"),
            email,
            valid,
            owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        )
    return {
        "contact_verified": valid,
        "contact_attempts": state.get("contact_attempts", 0) + 1,
        "research_outcome": "awaiting_review" if valid else "no_usable_contact",
        "contact_email": email if valid else None,
    }


def research_cache(state: ResearcherState) -> Dict[str, Any]:
    seed_query = state.get("seed_query")
    mode = state.get("mode") or ("open_role" if seed_query else "speculative")
    if mode == "speculative" or not seed_query:
        return {"cache_key": None, "research_cache_hit": False}

    cache_key = research_cache_key(
        seed_query,
        None,
        state.get("target_seniority"),
        state.get("location_preference"),
        state.get("target_domain_keywords"),
    )
    updates: Dict[str, Any] = {"cache_key": cache_key, "research_cache_hit": False}
    if app_config.MOCK_MODE or state.get("retry_count", 0) > 0:
        return updates
    cached = get_cached_research(
        cache_key,
        run_id=state.get("research_run_id") or "unknown",
    )
    if cached:
        cached_company = cached.get("company_name")
        cached_domain = cached.get("company_domain")
        rejected_candidates = state.get("rejected_candidates") or []
        rejected_names = {
            normalize_company_name(c.get("company_name"))
            for c in rejected_candidates
            if c.get("company_name")
        }
        if cached_company and normalize_company_name(cached_company) in rejected_names:
            return updates
        block_reason = company_block_reason(
            cached_company,
            cached_domain,
            owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        )
        if block_reason:
            return updates
        updates.update(cached)
        updates["research_cache_hit"] = True
    return updates


def research_cache_router(state: ResearcherState) -> str:
    return "llm_evaluates" if state.get("research_cache_hit") else "tavilysearch"


def tavily_router(state: ResearcherState) -> str:
    return END if state.get("research_outcome") == "failed" else "formatingnode"


def human_evaluation(state: ResearcherState) -> Dict[str, Any]:
    """Human-in-the-loop checkpoint before finalizing research."""
    return {}


def _can_search_again(state: ResearcherState) -> bool:
    return state.get("search_attempt", 1) < state.get(
        "search_budget",
        app_config.SPECULATIVE_SEARCH_BUDGET,
    )


def llm_router(state: ResearcherState) -> str:
    if state.get("research_outcome") == "failed":
        return END
    if state.get("research_sufficient"):
        return "huntersearch"
    if state.get("mode") != "speculative":
        return END
    return "retry_company" if _can_search_again(state) else "finish_search"


def hunter_router(state: ResearcherState) -> str:
    if state.get("hunter_candidates"):
        return "trynextcandidate"
    if state.get("mode") == "speculative":
        return "retry_company" if _can_search_again(state) else "finish_search"
    return END


def contact_router(state: ResearcherState) -> str:
    if state.get("research_outcome") == "failed":
        return END
    if state.get("contact_verified"):
        return "human_evaluation"
    candidates = state.get("hunter_candidates") or []
    if (
        state.get("hunter_candidate_index", 0) < len(candidates)
        and state.get("contact_attempts", 0) < app_config.MAX_EMAIL_VERIFICATIONS
    ):
        return "trynextcandidate"
    if state.get("mode") == "speculative":
        return "retry_company" if _can_search_again(state) else "finish_search"
    return END


def retry_company(state: ResearcherState) -> Dict[str, Any]:
    return {
        "company_name": None,
        "company_domain": None,
        "contact_name": None,
        "contact_email": None,
        "job_role": None,
        "job_description": None,
        "research_summary": None,
        "hunter_candidates": [],
        "hunter_candidate_index": 0,
        "contact_attempts": 0,
        "contact_verified": False,
        "research_sufficient": False,
        "research_outcome": "searching_next",
        "retry_count": state.get("retry_count", 0) + (
            0 if state.get("retry_counted") else 1
        ),
        "retry_counted": False,
        "rejected_candidates": _record_rejected_candidate(state),
        "evaluator_feedback": None,
        "search_attempt": state.get("search_attempt", 1),
        "search_budget": state.get("search_budget", app_config.SPECULATIVE_SEARCH_BUDGET),
    }


def finish_search(state: ResearcherState) -> Dict[str, Any]:
    return {
        "research_outcome": "search_exhausted",
        "rejected_candidates": _record_rejected_candidate(state),
        "search_attempt": state.get("search_attempt", 1),
        "search_budget": state.get("search_budget", app_config.SPECULATIVE_SEARCH_BUDGET),
    }


def human_router(state: ResearcherState) -> str:
    decision = str(state.get("human_decision") or "").strip().lower()
    if decision == "approve":
        return END
    if decision == "reject":
        return END
    if decision == "pending":
        return END
    if decision not in {"approve", "reject", "pending"}:
        raise ValueError("Research decision must be approve, reject, or pending.")


def build_researcher_graph(checkpointer=None):
    """
    Build and compile the Researcher Subgraph.
    """
    builder = StateGraph(ResearcherState)

    builder.add_node("inputpromptformater", inputpromptformater)
    builder.add_node("research_cache", research_cache)
    builder.add_node("tavilysearch", tavilysearch)
    builder.add_node("formatingnode", formatingnode)
    builder.add_node("huntersearch", huntersearch)
    builder.add_node("trynextcandidate", trynextcandidate)
    builder.add_node("llm_evaluates", llm_evaluates)
    builder.add_node("verify_candidate", verify_candidate)
    builder.add_node("retry_company", retry_company)
    builder.add_node("finish_search", finish_search)
    builder.add_node("human_evaluation", human_evaluation)

    builder.add_edge(START, "inputpromptformater")
    builder.add_edge("inputpromptformater", "research_cache")
    builder.add_conditional_edges(
        "research_cache",
        research_cache_router,
        ["llm_evaluates", "tavilysearch"],
    )
    builder.add_conditional_edges("tavilysearch", tavily_router, ["formatingnode", END])
    builder.add_edge("formatingnode", "llm_evaluates")

    builder.add_conditional_edges(
        "llm_evaluates",
        llm_router,
        ["retry_company", "huntersearch", "finish_search", END],
    )
    builder.add_conditional_edges(
        "huntersearch",
        hunter_router,
        ["trynextcandidate", "retry_company", "finish_search", END],
    )
    builder.add_edge("trynextcandidate", "verify_candidate")
    builder.add_conditional_edges(
        "verify_candidate",
        contact_router,
        ["human_evaluation", "trynextcandidate", "retry_company", "finish_search", END],
    )
    builder.add_edge("retry_company", "inputpromptformater")
    builder.add_conditional_edges(
        "human_evaluation",
        human_router,
        [END],
    )

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(interrupt_before=["human_evaluation"], checkpointer=cp)


# Module-level default compiled graph
researcher_graph = build_researcher_graph()


from langchain_core.runnables import RunnableConfig


def run_researcher(
    state: OutreachState,
    config: Optional[RunnableConfig] = None,
    decision_callback: Optional[Callable[[Dict[str, Any]], str]] = None,
    graph=None,
) -> Dict[str, Any]:
    """
    Execute the Researcher subgraph and handle human-in-the-loop interrupts.
    """
    cfg = config or {}
    active_graph = graph or researcher_graph
    parent_thread_id = (
        (cfg.get("configurable", {}).get("thread_id") if isinstance(cfg, dict) else None)
        or (Path(state.get("run_dir")).name if state.get("run_dir") else "run")
    )
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_researcher"}}

    sub_input = {
        "messages": state.get("messages", []),
        "seed_query": state.get("company_name"),
        "candidate_profile": state.get("candidate_profile", ""),
        "mode": state.get("mode", "open_role" if state.get("company_name") else "speculative"),
        "target_seniority": state.get("target_seniority"),
        "target_domain_keywords": state.get("target_domain_keywords"),
        "location_preference": state.get("location_preference"),
        "retry_count": 0,
        "max_retries": state.get("max_retries", app_config.MAX_RETRIES),
        "search_attempt": 0,
        "search_budget": state.get(
            "search_budget",
            app_config.SPECULATIVE_SEARCH_BUDGET,
        ),
        "retry_counted": False,
        "rejected_candidates": [],
        "research_run_id": (cfg.get("configurable", {}).get("thread_id") if isinstance(cfg, dict) else None) or "run",
        "owner_id": state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        "contact_attempts": 0,
        "research_outcome": None,
        "research_error": None,
        "research_cache_hit": False,
    }

    checkpoint = active_graph.get_state(clean_config)
    if checkpoint.next:
        result = checkpoint.values
    else:
        result = active_graph.invoke(sub_input, clean_config)

    while active_graph.get_state(clean_config).next:
        current_values = active_graph.get_state(clean_config).values
        print("\n--- RESEARCH SUMMARY FOR REVIEW ---")
        print(f"Company: {current_values.get('company_name')}")
        print(f"Contact: {current_values.get('contact_name')} <{current_values.get('contact_email')}>")
        print(f"Role: {current_values.get('job_role')}")
        print(f"Summary: {current_values.get('research_summary')}")

        if decision_callback:
            decision = decision_callback(current_values)
        else:
            decision = input("Approve this research? (approve/reject): ").strip().lower()

        decision = str(decision or "").strip().lower()
        if decision == "pending":
            result = current_values
            break
        if decision not in {"approve", "reject"}:
            raise ValueError("Research decision must be approve, reject, or pending.")
        active_graph.update_state(clean_config, {"human_decision": decision})
        result = active_graph.invoke(None, clean_config)

    decision = str(result.get("human_decision") or "").strip().lower()
    outcome = (
        "approved" if decision == "approve"
        else "rejected" if decision == "reject"
        else result.get("research_outcome") or "unknown"
    )
    company = result.get("company_name")
    domain = result.get("company_domain")
    final = {
        "company_name": company,
        "company_domain": domain,
        "contact_name": result.get("contact_name"),
        "contact_email": result.get("contact_email"),
        "job_role": result.get("job_role"),
        "job_description": result.get("job_description"),
        "research_summary": result.get("research_summary"),
        "research_approved": outcome == "approved",
        "research_outcome": outcome,
        "retry_count": result.get("retry_count", state.get("retry_count", 0)),
        "search_attempt": result.get("search_attempt", 1),
        "search_budget": result.get("search_budget", app_config.SPECULATIVE_SEARCH_BUDGET),
        "candidates_discovered": max(len(result.get("rejected_candidates") or []) + (1 if outcome == "approved" else 0), int(result.get("search_attempt", 1))),
        "candidates_evaluated": len(result.get("rejected_candidates") or []) + (1 if outcome == "approved" else 0),
        "candidates_accepted": 1 if outcome == "approved" else 0,
        "rejected_candidates": result.get("rejected_candidates") or [],
        "research_error": result.get("research_error"),
        "evaluator_feedback": result.get("evaluator_feedback"),
        "eligibility_reason": result.get("eligibility_reason") or result.get("evaluator_feedback"),
        "company_relevant": result.get("company_relevant", True),
        "role_suitable": result.get("role_suitable", True),
        "seniority_match": result.get("seniority_match", True),
        "experience_required": result.get("experience_required"),
        "research_run_id": sub_input["research_run_id"],
    }
    run_id = sub_input["research_run_id"]
    if outcome == "rejected" and company:
        add_exclusion(sub_input["owner_id"], company, domain, "Rejected by user: Candidate rejected by user.")
    record_research_run(
        run_id,
        company,
        result.get("job_role"),
        outcome,
        owner_id=sub_input["owner_id"],
        domain=domain,
        rejection_reason=(
            result.get("evaluator_feedback")
            if outcome in {"rejected", "no_matching_role", "no_usable_contact", "search_exhausted", "failed"}
            else None
        ),
        source_urls=re.findall(r"https?://[^\s)\]]+", result.get("tavily_findings") or ""),
        provider_usage=get_usage_summary(run_id),
    )
    return final
