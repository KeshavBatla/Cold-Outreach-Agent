"""
Researcher Agent module.
Generates targeted queries, searches via Tavily, extracts structured details,
ranks contacts via Hunter.io, evaluates role fit, and pauses for human verification.
"""

from typing import Dict, Any, Optional, Callable
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config
from graph.state import ResearcherState, OutreachState
from prompts.candidate import CANDIDATE_PROFILE
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
from utils.helpers import extract_text, is_already_mailed, PLACEHOLDER_VALUES, EMAIL_REGEX


def inputpromptformater(state: ResearcherState) -> Dict[str, Any]:
    llm = config.get_llm()
    profile = state.get("candidate_profile", CANDIDATE_PROFILE)
    seed_query = state.get("seed_query", "")
    mode = "open_role" if seed_query else "speculative"

    system_text = RESEARCHER_QUERY_SYSTEM_PROMPT.format(
        profile=profile, mode=mode, seed_query=seed_query
    )
    response = llm.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=RESEARCHER_QUERY_HUMAN_PROMPT),
        ]
    )
    return {"search_query": extract_text(response.content), "mode": mode}


def tavilysearch(state: ResearcherState) -> Dict[str, Any]:
    query = extract_text(state.get("search_query", ""))
    findings = search_tavily(query)
    return {"tavily_findings": findings}


def formatingnode(state: ResearcherState) -> Dict[str, Any]:
    llm = config.get_llm()
    merged = state.get("tavily_findings") or "No findings available."
    research_extractor = get_research_extractor(llm)

    messages = [
        SystemMessage(content=EXTRACTION_SYSTEM_PROMPT),
        HumanMessage(content=merged),
    ]
    result = research_extractor.invoke({"messages": messages})
    extracted: ResearchExtraction = result["responses"][0]
    company_name = extracted.company_name

    if company_name and company_name.strip().lower() in PLACEHOLDER_VALUES:
        company_name = None

    if not company_name and extracted.research_summary:
        company_name_extractor = get_company_name_extractor(llm)
        fallback = company_name_extractor.invoke(
            {
                "messages": [
                    SystemMessage(content=COMPANY_NAME_FALLBACK_PROMPT),
                    HumanMessage(content=extracted.research_summary),
                ]
            }
        )
        company_name = fallback["responses"][0].company_name
        print(f"[formatingnode] company_name was blank, fallback found: {company_name!r}")

    if company_name and is_already_mailed(company_name):
        print(f"[formatingnode] Skipping '{company_name}': Already in contacted list.")
        company_name = None

    return {
        "company_name": company_name,
        "contact_name": extracted.contact_name,
        "contact_email": extracted.contact_email,
        "job_role": extracted.job_role,
        "job_description": extracted.job_description,
        "research_summary": extracted.research_summary,
    }


def huntersearch(state: ResearcherState) -> Dict[str, Any]:
    company = state.get("company_name")
    if not company:
        return {"hunter_candidates": [], "hunter_candidate_index": 0}

    candidates = search_domain_contacts(company)
    if not candidates:
        return {"hunter_candidates": [], "hunter_candidate_index": 0}

    tavily_email = state.get("contact_email")
    if tavily_email and EMAIL_REGEX.match(tavily_email):
        candidates.insert(
            0,
            {
                "name": state.get("contact_name"),
                "email": tavily_email,
                "position": "from Tavily",
            },
        )

    return {"hunter_candidates": candidates, "hunter_candidate_index": 0}


def trynextcandidate(state: ResearcherState) -> Dict[str, Any]:
    candidates = state.get("hunter_candidates") or []
    idx = state.get("hunter_candidate_index", 0)
    if idx >= len(candidates):
        print(f"[trynextcandidate] Exhausted all {len(candidates)} candidates for this company.")
        return {"contact_name": None, "contact_email": None}

    pick = candidates[idx]
    print(f"[trynextcandidate] Trying candidate {idx + 1}/{len(candidates)}: {pick}")
    return {
        "contact_name": pick["name"],
        "contact_email": pick["email"],
        "hunter_candidate_index": idx + 1,
    }


def llm_evaluates(state: ResearcherState) -> Dict[str, Any]:
    has_company = bool(state.get("company_name"))
    contact_email = state.get("contact_email")
    email_valid = verify_email(contact_email) if contact_email else False

    if has_company and not email_valid:
        candidates = state.get("hunter_candidates") or []
        idx = state.get("hunter_candidate_index", 0)
        if idx >= len(candidates):
            return {
                "contact_email": None,
                "company_name": None,
                "contact_name": None,
                "research_summary": None,
                "job_role": None,
                "job_description": None,
                "contact_failed_only": False,
                "hunter_candidates": [],
                "hunter_candidate_index": 0,
                "evaluator_feedback": "All discovered contacts failed verification — trying a different company.",
                "research_sufficient": False,
                "retry_count": state.get("retry_count", 0) + 1,
            }
        return {
            "contact_email": None,
            "contact_failed_only": True,
            "evaluator_feedback": "That contact failed verification — trying the next one.",
            "research_sufficient": False,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    if not has_company:
        return {
            "contact_failed_only": False,
            "hunter_only_attempts": 0,
            "evaluator_feedback": "No company identified — full retry needed.",
            "research_sufficient": False,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    llm = config.get_llm()
    research_evaluator = get_research_evaluator(llm)

    messages = [
        SystemMessage(content=RESEARCH_EVALUATION_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Job role: {state.get('job_role')}\n"
                f"Job description: {state.get('job_description')}\n"
                f"Research summary: {state.get('research_summary')}"
            )
        ),
    ]
    result = research_evaluator.invoke({"messages": messages})
    evaluation: ResearchEvaluation = result["responses"][0]
    print(
        f"[llm_evaluates] ROLE FIT — job_role={state.get('job_role')!r}, "
        f"is_sufficient={evaluation.is_sufficient}, feedback={evaluation.feedback}"
    )

    updates = {
        "evaluator_feedback": evaluation.feedback,
        "research_sufficient": evaluation.is_sufficient,
        "contact_failed_only": False,
        "hunter_only_attempts": 0,
    }
    if not evaluation.is_sufficient:
        updates["retry_count"] = state.get("retry_count", 0) + 1
    return updates


def human_evaluation(state: ResearcherState) -> Dict[str, Any]:
    """Human-in-the-loop checkpoint before finalizing research."""
    return {}


def llm_router(state: ResearcherState) -> str:
    if state.get("research_sufficient"):
        return "human_evaluation"
    if state.get("retry_count", 0) >= state.get("max_retries", config.MAX_RETRIES):
        return "human_evaluation"
    return "trynextcandidate" if state.get("contact_failed_only") else "inputpromptformater"


def human_router(state: ResearcherState) -> str:
    if state.get("human_decision") == "approve":
        return END
    return "trynextcandidate" if state.get("contact_failed_only") else "inputpromptformater"


def build_researcher_graph(checkpointer=None):
    """
    Build and compile the Researcher Subgraph.
    """
    builder = StateGraph(ResearcherState)

    builder.add_node("inputpromptformater", inputpromptformater)
    builder.add_node("tavilysearch", tavilysearch)
    builder.add_node("formatingnode", formatingnode)
    builder.add_node("huntersearch", huntersearch)
    builder.add_node("trynextcandidate", trynextcandidate)
    builder.add_node("llm_evaluates", llm_evaluates)
    builder.add_node("human_evaluation", human_evaluation)

    builder.add_edge(START, "inputpromptformater")
    builder.add_edge("inputpromptformater", "tavilysearch")
    builder.add_edge("tavilysearch", "formatingnode")
    builder.add_edge("formatingnode", "huntersearch")
    builder.add_edge("huntersearch", "trynextcandidate")
    builder.add_edge("trynextcandidate", "llm_evaluates")

    builder.add_conditional_edges(
        "llm_evaluates",
        llm_router,
        ["inputpromptformater", "trynextcandidate", "human_evaluation"],
    )
    builder.add_conditional_edges(
        "human_evaluation",
        human_router,
        ["inputpromptformater", "trynextcandidate", END],
    )

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(interrupt_before=["human_evaluation"], checkpointer=cp)


# Module-level default compiled graph
researcher_graph = build_researcher_graph()


def run_researcher(
    state: OutreachState,
    cfg: Dict[str, Any],
    decision_callback: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    """
    Execute the Researcher subgraph and handle human-in-the-loop interrupts.
    """
    parent_thread_id = cfg["configurable"]["thread_id"]
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_researcher"}}

    sub_input = {
        "messages": state.get("messages", []),
        "seed_query": state.get("company_name"),
        "candidate_profile": state.get("candidate_profile", CANDIDATE_PROFILE),
        "mode": state.get("mode", "open_role" if state.get("company_name") else "speculative"),
        "retry_count": 0,
        "max_retries": state.get("max_retries", config.MAX_RETRIES),
    }

    result = researcher_graph.invoke(sub_input, clean_config)

    while researcher_graph.get_state(clean_config).next:
        current_values = researcher_graph.get_state(clean_config).values
        print("\n--- RESEARCH SUMMARY FOR REVIEW ---")
        print(f"Company: {current_values.get('company_name')}")
        print(f"Contact: {current_values.get('contact_name')} <{current_values.get('contact_email')}>")
        print(f"Role: {current_values.get('job_role')}")
        print(f"Summary: {current_values.get('research_summary')}")

        if decision_callback:
            decision = decision_callback(current_values)
        else:
            decision = input("Approve this research? (approve/reject): ").strip().lower()

        researcher_graph.update_state(clean_config, {"human_decision": decision})
        result = researcher_graph.invoke(None, clean_config)

    return {
        "company_name": result.get("company_name"),
        "contact_name": result.get("contact_name"),
        "contact_email": result.get("contact_email"),
        "job_role": result.get("job_role"),
        "job_description": result.get("job_description"),
        "research_summary": result.get("research_summary"),
        "research_approved": result.get("human_decision") == "approve",
        "retry_count": state.get("retry_count", 0) + (0 if result.get("human_decision") == "approve" else 1),
    }

