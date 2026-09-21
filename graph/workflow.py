"""
Workflow orchestration module.
Constructs and executes the master Cold Outreach LangGraph multi-agent pipeline.
"""

import uuid
from typing import Dict, Any, Optional, Callable

import config
from graph.state import OutreachState
from prompts.candidate import CANDIDATE_PROFILE
from agents.manager import manager_graph, build_manager_graph
from agents.researcher import build_researcher_graph
from agents.cover_writer import build_coverwriter_graph
from agents.email_writer import build_emailwriter_graph
from agents.email_drafter import build_emaildrafter_graph


def create_initial_outreach_state(
    company_name: Optional[str] = None,
    mode: Optional[str] = None,
    candidate_profile: Optional[str] = None,
    resume_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initialize a fresh OutreachState object with clean baseline parameters.
    """
    resolved_mode = mode or ("open_role" if company_name else "speculative")
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
        "candidate_profile": candidate_profile or CANDIDATE_PROFILE,
        "mode": resolved_mode,
        "resume_path": resume_path or config.DEFAULT_RESUME_PATH,
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
) -> Dict[str, Any]:
    """
    Programmatic entrypoint to run the end-to-end outreach pipeline.
    """
    run_id = thread_id or f"manager_run_{uuid.uuid4().hex[:8]}"
    manager_config = {"configurable": {"thread_id": run_id}}

    initial_state = create_initial_outreach_state(
        company_name=company_name,
        mode=mode,
    )

    print("==================================================")
    print(f"STARTING OUTREACH RUN — Thread: {run_id}")
    if company_name:
        print(f"Target Seed Company: {company_name}")
    else:
        print("Mode: Speculative AI Opportunity Discovery")
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

