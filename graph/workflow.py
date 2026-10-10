"""
Workflow orchestration module.
Constructs and executes the master Cold Outreach LangGraph multi-agent pipeline.
"""

import uuid
from typing import Dict, Any, Optional

import config
from graph.state import OutreachState
from agents.manager import manager_graph, build_manager_graph
from agents.researcher import build_researcher_graph
from agents.cover_writer import build_coverwriter_graph
from agents.email_writer import build_emailwriter_graph
from agents.email_drafter import build_emaildrafter_graph
from utils.cv_parser import generate_candidate_profile


def create_initial_outreach_state(
    company_name: Optional[str] = None,
    mode: Optional[str] = None,
    candidate_profile: Optional[str] = None,
    resume_path: Optional[str] = None,
    raw_cv_text: Optional[str] = None,
    example_cover_letter: Optional[str] = None,
    target_seniority: Optional[str] = None,
    target_domain_keywords: Optional[str] = None,
    location_preference: Optional[str] = None,
    candidate_name: Optional[str] = None,
    run_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initialize a fresh OutreachState object with clean baseline parameters.
    Raises ValueError if resume_path is missing.
    """
    if not resume_path:
        raise ValueError("resume_path is required to initialize outreach state.")

    resolved_mode = mode or ("open_role" if company_name else "speculative")

    # Generate candidate profile from raw CV text if not provided but CV text is available
    final_candidate_profile = candidate_profile
    if final_candidate_profile is None and raw_cv_text is not None:
        final_candidate_profile = generate_candidate_profile(raw_cv_text)

    # If still None, use empty string
    if final_candidate_profile is None:
        final_candidate_profile = ""

    return {
        "messages": [("user", "Start new cold outreach cycle")],
        "company_name": company_name,
        "contact_name": None,
        "contact_email": None,
        "job_role": None,
        "job_description": None,
        "research_summary": None,
        "cover_letter": None,
        "email_body": None,
        "subject": None,
        "candidate_name": candidate_name,
        "candidate_profile": final_candidate_profile,
        "mode": resolved_mode,
        "resume_path": resume_path,
        "run_dir": run_dir,
        "raw_cv_text": raw_cv_text,
        "example_cover_letter": example_cover_letter,
        "target_seniority": target_seniority,
        "target_domain_keywords": target_domain_keywords,
        "location_preference": location_preference,
        "research_approved": False,
        "cover_letter_approved": False,
        "email_approved": False,
        "final_email_sent": False,
        "retry_count": 0,
        "max_retries": config.MAX_RETRIES,
        "cover_letter_attempts": 0,
        "email_attempts": 0,
        "excel_updates_pending": None,
    }


def run_outreach_cycle(
    company_name: Optional[str] = None,
    mode: Optional[str] = None,
    thread_id: Optional[str] = None,
    candidate_profile: Optional[str] = None,
    resume_path: Optional[str] = None,
    raw_cv_text: Optional[str] = None,
    example_cover_letter: Optional[str] = None,
    target_seniority: Optional[str] = None,
    target_domain_keywords: Optional[str] = None,
    location_preference: Optional[str] = None,
    candidate_name: Optional[str] = None,
    run_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Programmatic entrypoint to run the end-to-end outreach pipeline.
    """
    run_id = thread_id or f"manager_run_{uuid.uuid4().hex[:8]}"
    manager_config = {"configurable": {"thread_id": run_id}}

    resolved_run_dir = run_dir or str(config.get_run_dir(run_id))

    initial_state = create_initial_outreach_state(
        company_name=company_name,
        mode=mode,
        candidate_profile=candidate_profile,
        resume_path=resume_path,
        raw_cv_text=raw_cv_text,
        example_cover_letter=example_cover_letter,
        target_seniority=target_seniority,
        target_domain_keywords=target_domain_keywords,
        location_preference=location_preference,
        candidate_name=candidate_name,
        run_dir=resolved_run_dir,
    )

    print("==================================================")
    print(f"STARTING OUTREACH RUN — Thread: {run_id}")
    if company_name:
        print(f"Target Seed Company: {company_name}")
    else:
        print("Mode: Speculative AI Opportunity Discovery")
    print(f"Run Directory: {resolved_run_dir}")
    print("==================================================")

    final_state = manager_graph.invoke(initial_state, manager_config)

    print("\n==================================================")
    print("RUN COMPLETE")
    print("==================================================")
    print(f"Company: {final_state.get('company_name')}")
    print(f"Contact: {final_state.get('contact_name')} <{final_state.get('contact_email')}>")
    print(f"Role: {final_state.get('job_role')}")
    print(f"Cover letter approved: {final_state.get('cover_letter_approved')}")
    print(f"Email approved: {final_state.get('email_approved')}")
    print(f"Final email sent: {final_state.get('final_email_sent')}")

    return final_state
