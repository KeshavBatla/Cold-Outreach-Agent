"""
LangGraph state definitions for supervisor and subgraphs.
"""

from typing import Optional, List, Dict, Any
from langgraph.graph import MessagesState


class OutreachState(MessagesState):
    """
    Master state passed across supervisor and all subgraphs.
    """
    company_name: Optional[str]
    contact_name: Optional[str]
    contact_email: Optional[str]
    job_role: Optional[str]
    job_description: Optional[str]
    research_summary: Optional[str]
    company_domain: Optional[str]
    research_run_id: Optional[str]

    cover_letter: Optional[str]
    email_body: Optional[str]
    subject: Optional[str]

    candidate_name: Optional[str]
    candidate_profile: Optional[str]
    mode: Optional[str]
    resume_path: Optional[str]
    run_dir: Optional[str]

    # Generalized candidate inputs
    raw_cv_text: Optional[str]          # Extracted text from uploaded CV
    example_cover_letter: Optional[str] # Uploaded example cover letter text
    cv_artifact_id: Optional[str]
    example_letter_artifact_id: Optional[str]
    target_seniority: Optional[str]     # e.g., "fresher/intern", "experienced"
    target_domain_keywords: Optional[str] # e.g., "agentic AI, LLM applications"
    location_preference: Optional[str]  # e.g., "remote", "India-based"

    research_approved: bool
    research_outcome: Optional[str]
    research_error: Optional[str]
    cache_key: Optional[str]
    research_cache_hit: bool
    cover_letter_approved: bool
    email_approved: bool
    final_email_sent: bool
    delivery_status: Optional[str]
    usage_summary: Optional[Dict[str, Any]]

    cover_letter_attempts: int
    email_attempts: int
    retry_count: int
    max_retries: int

    excel_updates_pending: Optional[List[Dict[str, Any]]]


class ResearcherState(MessagesState):
    """
    State scoped to the researcher agent subgraph.
    """
    candidate_profile: str
    mode: str
    seed_query: Optional[str]
    search_query: Optional[str]
    tavily_findings: Optional[str]
    hunter_findings: Optional[str]
    company_name: Optional[str]
    contact_name: Optional[str]
    contact_email: Optional[str]
    job_role: Optional[str]
    job_description: Optional[str]
    research_summary: Optional[str]
    target_seniority: Optional[str]
    target_domain_keywords: Optional[str]
    location_preference: Optional[str]
    research_sufficient: bool
    contact_failed_only: bool
    hunter_only_attempts: int
    evaluator_feedback: Optional[str]
    human_decision: Optional[str]
    research_outcome: Optional[str]
    research_error: Optional[str]
    cache_key: Optional[str]
    research_cache_hit: bool
    retry_count: int
    max_retries: int
    hunter_candidates: Optional[list]
    hunter_candidate_index: int
    company_domain: Optional[str]
    contact_verified: Optional[bool]
    contact_attempts: int
    owner_id: Optional[str]
    research_run_id: Optional[str]


class CoverwriterState(MessagesState):
    """
    State scoped to the cover letter writer agent subgraph.
    """
    company_name: Optional[str]
    job_role: Optional[str]
    job_description: Optional[str]
    research_summary: Optional[str]
    research_run_id: Optional[str]
    raw_cv_text: Optional[str]
    example_cover_letter: Optional[str]
    cv_artifact_id: Optional[str]
    example_letter_artifact_id: Optional[str]
    cover_letter: Optional[str]
    evaluator_feedback: Optional[str]
    letter_sufficient: Optional[bool]
    human_decision: Optional[str]
    retry_count: int
    max_retries: int


class EmailwriterState(MessagesState):
    """
    State scoped to the cold email writer agent subgraph.
    """
    company_name: Optional[str]
    contact_name: Optional[str]
    job_role: Optional[str]
    research_summary: Optional[str]
    cover_letter: Optional[str]
    candidate_name: Optional[str]
    candidate_profile: Optional[str]
    research_run_id: Optional[str]
    email_body: Optional[str]
    evaluator_feedback: Optional[str]
    email_sufficient: Optional[bool]
    human_decision: Optional[str]
    retry_count: int
    max_retries: int


class EmaildrafterState(MessagesState):
    """
    State scoped to the email drafter and sender agent subgraph.
    """
    company_name: Optional[str]
    job_role: Optional[str]
    contact_email: Optional[str]
    contact_name: Optional[str]
    email_body: Optional[str]
    cover_letter: Optional[str]
    subject: Optional[str]
    candidate_name: Optional[str]
    resume_path: Optional[str]
    run_dir: Optional[str]
    company_domain: Optional[str]
    research_run_id: Optional[str]
    owner_id: Optional[str]
    human_decision: Optional[str]
    sent: bool
    delivery_status: Optional[str]
    reservation_token: Optional[str]
    excel_log_entry: Optional[Dict[str, Any]]
