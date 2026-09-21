"""
Emailwriter Agent module.
Generates concise, personalized 3-5 sentence cold outreach email bodies,
with automated LLM evaluation and refinement feedback.
"""

from typing import Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config
from graph.state import EmailwriterState, OutreachState
from prompts.email_writer import (
    EMAIL_WRITER_SYSTEM_TEMPLATE,
    EMAIL_WRITER_HUMAN_PROMPT,
    EMAIL_EVALUATION_TEMPLATE,
)
from evaluation.llm_evaluator import EmailEvaluation, get_email_evaluator
from utils.helpers import extract_text


def emailcontent(state: EmailwriterState) -> Dict[str, Any]:
    company = state.get("company_name")
    role = state.get("job_role")
    if not company or not role:
        return {
            "email_body": None,
            "evaluator_feedback": "Missing company_name or job_role input — cannot write a targeted email.",
            "email_sufficient": False,
        }

    contact_name = state.get("contact_name") or "Hiring Team"
    research = state.get("research_summary") or ""
    cover_letter = state.get("cover_letter") or ""
    feedback = state.get("evaluator_feedback")

    system_text = EMAIL_WRITER_SYSTEM_TEMPLATE.format(
        company=company,
        contact_name=contact_name,
        role=role,
        research=research,
        cover_letter=cover_letter,
    )

    if feedback:
        system_text += f"\nPrevious draft's feedback (fix this): {feedback}\n"

    llm = config.get_llm()
    response = llm.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=EMAIL_WRITER_HUMAN_PROMPT),
        ]
    )
    return {"email_body": extract_text(response.content)}


def llm_evaluator_email(state: EmailwriterState) -> Dict[str, Any]:
    email_body = extract_text(state.get("email_body", ""))
    current_retry = state.get("retry_count", 0)

    if not email_body:
        return {
            "evaluator_feedback": "No email body was generated.",
            "email_sufficient": False,
            "retry_count": current_retry + 1,
        }

    llm = config.get_llm()
    evaluator = get_email_evaluator(llm)

    messages = [
        SystemMessage(
            content=EMAIL_EVALUATION_TEMPLATE.format(
                company=state.get("company_name"), role=state.get("job_role")
            )
        ),
        HumanMessage(content=email_body),
    ]
    result = evaluator.invoke({"messages": messages})
    evaluation: EmailEvaluation = result["responses"][0]

    updates = {
        "evaluator_feedback": evaluation.feedback,
        "email_sufficient": evaluation.is_sufficient,
    }
    if not evaluation.is_sufficient:
        updates["retry_count"] = current_retry + 1
    return updates


def llm_router_email(state: EmailwriterState) -> str:
    if state.get("email_sufficient"):
        return END
    if state.get("retry_count", 0) >= state.get("max_retries", config.MAX_RETRIES):
        return END
    return "emailcontent"


def build_emailwriter_graph(checkpointer=None):
    """
    Build and compile the Emailwriter Subgraph.
    """
    builder = StateGraph(EmailwriterState)
    builder.add_node("emailcontent", emailcontent)
    builder.add_node("llm_evaluator", llm_evaluator_email)
    builder.add_edge(START, "emailcontent")
    builder.add_edge("emailcontent", "llm_evaluator")
    builder.add_conditional_edges("llm_evaluator", llm_router_email, ["emailcontent", END])

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=cp)


emailwriter_graph = build_emailwriter_graph()


def run_emailwriter(state: OutreachState, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute the Emailwriter subgraph.
    """
    parent_thread_id = cfg["configurable"]["thread_id"]
    attempt_num = state.get("email_attempts", 0)
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_email_{attempt_num}"}}

    sub_input = {
        "messages": state.get("messages", []),
        "company_name": state.get("company_name"),
        "contact_name": state.get("contact_name"),
        "job_role": state.get("job_role"),
        "research_summary": state.get("research_summary"),
        "cover_letter": state.get("cover_letter"),
        "retry_count": 0,
        "max_retries": state.get("max_retries", config.MAX_RETRIES),
    }
    result = emailwriter_graph.invoke(sub_input, clean_config)
    print("\n--- EMAIL DRAFT ---")
    print(result.get("email_body"))
    print("--- EVALUATOR FEEDBACK ---")
    print(result.get("evaluator_feedback"))

    return {
        "email_body": result.get("email_body"),
        "email_approved": bool(result.get("email_sufficient")),
        "email_attempts": attempt_num + 1,
    }

