"""
Emaildrafter Agent module.
Generates tailored email subjects, packages attachments (resume and rendered cover letter PDF),
pauses for final human approval, and transmits the email via Gmail API.
"""

from pathlib import Path
from typing import Dict, Any, Optional, Callable
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config as app_config
from graph.state import EmaildrafterState, OutreachState
from prompts.email_writer import EMAIL_SUBJECT_SYSTEM_TEMPLATE
from evaluation.llm_evaluator import EmailSubject, get_subject_generator
from tools.pdf_generator import text_to_pdf
from tools.gmail import send_email
from persistence import complete_outreach, mark_outreach_sending, normalize_email, reserve_outreach
from utils.helpers import EMAIL_REGEX
from utils.observability import invoke_with_usage
from persistence import get_usage_summary


def emaildrafter(state: EmaildrafterState) -> Dict[str, Any]:
    company = state.get("company_name")
    role = state.get("job_role")
    if not company or not role:
        return {"subject": None}

    if app_config.MOCK_MODE:
        return {"subject": f"Interest in {role} at {company}"}
    llm = app_config.get_llm()
    subject_generator = get_subject_generator(llm)

    candidate_name = state.get("candidate_name") or "Applicant"
    result = invoke_with_usage(
        subject_generator.invoke,
        {
            "messages": [
                SystemMessage(
                    content=EMAIL_SUBJECT_SYSTEM_TEMPLATE.format(
                        role=role, company=company, candidate_name=candidate_name
                    )
                ),
                HumanMessage(content="Generate it."),
            ]
        },
        provider="gemini",
        operation="email_subject_generation",
        model=app_config.MODEL_NAME,
        run_id=state.get("research_run_id") or "unknown",
    )
    subject: EmailSubject = result["responses"][0]
    return {"subject": subject.subject}


def human_evaluation_draft(state: EmaildrafterState) -> Dict[str, Any]:
    """Human-in-the-loop checkpoint before actually sending email."""
    return {}


def human_router_draft(state: EmaildrafterState) -> str:
    decision = str(state.get("human_decision") or "").strip().lower()
    if decision == "approve":
        return "emailsender"
    if decision in {"reject", "pending"}:
        return END
    raise ValueError("Email decision must be approve, reject, or pending.")


def emailsender(state: EmaildrafterState) -> Dict[str, Any]:
    to_email = state.get("contact_email")
    company_name = state.get("company_name", "Target Company")
    role = state.get("job_role", "Target Role")
    subject = state.get("subject") or f"Application for {role} at {company_name}"
    body = state.get("email_body") or ""
    current_status = state.get("delivery_status")

    if current_status in {"sent", "dry_run", "failed", "unknown", "blocked", "simulated"}:
        return {
            "sent": current_status == "sent",
            "delivery_status": current_status,
            "excel_log_entry": {
                "company": company_name,
                "contact_email": to_email,
                "role": role,
                "status": current_status,
            },
        }

    if state.get("human_decision") != "approve":
        raise PermissionError("The email cannot be sent without explicit user approval.")
    if not to_email or not EMAIL_REGEX.match(to_email):
        return {
            "sent": False,
            "delivery_status": "failed",
            "excel_log_entry": {
                "company": company_name,
                "status": "failed - missing or invalid recipient email",
            },
        }
    if not body.strip() or not subject.strip():
        return {
            "sent": False,
            "delivery_status": "failed",
            "excel_log_entry": {
                "company": company_name,
                "status": "failed - reviewed subject and body are required",
            },
        }

    attachments = []

    # 1. Attach Resume / CV
    resume_path = state.get("resume_path")
    if resume_path and Path(resume_path).exists():
        attachments.append(resume_path)
    else:
        return {
            "sent": False,
            "delivery_status": "failed",
            "excel_log_entry": {
                "company": company_name,
                "status": f"failed - resume attachment not found: {resume_path}",
            },
        }

    # 2. Render and attach Cover Letter PDF into run_dir
    cover_letter_text = state.get("cover_letter")
    if cover_letter_text:
        clean_company = company_name.replace(" ", "_").replace("/", "_")
        run_dir = state.get("run_dir")
        if run_dir:
            pdf_path = Path(run_dir) / f"cover_letter_{clean_company}.pdf"
        else:
            pdf_path = Path(app_config.COVER_LETTER_DIR) / f"cover_letter_{clean_company}.pdf"
        try:
            text_to_pdf(cover_letter_text, str(pdf_path), title=f"Cover Letter — {company_name}")
            attachments.append(str(pdf_path))
        except Exception as e:
            return {
                "sent": False,
                "delivery_status": "failed",
                "excel_log_entry": {
                    "company": company_name,
                    "status": f"failed - could not generate cover letter attachment: {e}",
                },
            }
    else:
        return {
            "sent": False,
            "delivery_status": "failed",
            "excel_log_entry": {
                "company": company_name,
                "status": "failed - cover letter attachment is missing",
            },
        }

    reservation_token = None
    if not app_config.MOCK_MODE:
        reservation_token, blocked_reason = reserve_outreach(
            run_id=state.get("research_run_id") or state.get("owner_id") or "local-run",
            company_name=company_name,
            domain=state.get("company_domain"),
            recipient_email=normalize_email(to_email),
            owner_id=state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        )
        if not reservation_token:
            return {
                "sent": False,
                "delivery_status": "blocked",
                "excel_log_entry": {
                    "company": company_name,
                    "contact_email": to_email,
                    "role": role,
                    "status": f"blocked - {blocked_reason}",
                },
            }
        mark_outreach_sending(reservation_token)

    # Sending uses exactly the reviewed recipient, subject, body, and attachment paths.
    send_result = send_email(
        to_email=to_email,
        subject=subject,
        body=body,
        attachment_paths=attachments,
        dry_run=app_config.DRY_RUN,
        approved=True,
        run_id=state.get("research_run_id") or "unknown",
    )

    delivery_status = (
        "sent" if send_result.get("sent")
        else "simulated" if send_result.get("simulated")
        else "dry_run" if send_result.get("dry_run")
        else send_result.get("delivery_status") or "failed"
    )
    if reservation_token:
        complete_outreach(
            reservation_token,
            delivery_status if delivery_status in {"sent", "dry_run", "failed", "unknown"} else "failed",
            message_id=send_result.get("message_id"),
            error=send_result.get("error"),
        )

    if send_result.get("sent"):
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": "sent",
            "gmail_message_id": send_result.get("message_id"),
        }
        return {"sent": True, "delivery_status": "sent", "excel_log_entry": log_entry}
    if send_result.get("simulated"):
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": "simulated - no email transmitted",
        }
        return {"sent": False, "delivery_status": "simulated", "excel_log_entry": log_entry}
    if send_result.get("dry_run"):
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": "dry_run",
            "gmail_message_id": "dry-run-preview",
        }
        return {"sent": False, "delivery_status": "dry_run", "excel_log_entry": log_entry}

    error_text = send_result.get("error") or "unknown send error"
    log_entry = {
        "company": company_name,
        "contact_email": to_email,
        "role": role,
        "status": f"failed - {error_text}",
    }
    failed_status = "unknown" if send_result.get("delivery_status") == "unknown" else "failed"
    return {"sent": False, "delivery_status": failed_status, "excel_log_entry": log_entry}


def excelentry(state: EmaildrafterState) -> Dict[str, Any]:
    """
    Placeholder logging node preserved from notebook implementation.
    Real Sheets/Excel synchronization is planned for a future integration.
    """
    log = state.get("excel_log_entry") or {}
    print(f"[excelentry placeholder] would log: {log}")
    return {}


def build_emaildrafter_graph(checkpointer=None):
    """
    Build and compile the Emaildrafter Subgraph with human-in-the-loop interrupt.
    """
    builder = StateGraph(EmaildrafterState)
    builder.add_node("emaildrafter", emaildrafter)
    builder.add_node("human_evaluation", human_evaluation_draft)
    builder.add_node("emailsender", emailsender)
    builder.add_node("excelentry", excelentry)

    builder.add_edge(START, "emaildrafter")
    builder.add_edge("emaildrafter", "human_evaluation")
    builder.add_conditional_edges("human_evaluation", human_router_draft, ["emaildrafter", "emailsender"])
    builder.add_edge("emailsender", "excelentry")
    builder.add_edge("excelentry", END)

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(interrupt_before=["human_evaluation"], checkpointer=cp)


emaildrafter_graph = build_emaildrafter_graph()


from langchain_core.runnables import RunnableConfig


def run_emaildrafter(
    state: OutreachState,
    config: Optional[RunnableConfig] = None,
    decision_callback: Optional[Callable[[Dict[str, Any]], str]] = None,
    graph=None,
) -> Dict[str, Any]:
    """
    Execute the Emaildrafter subgraph and handle human approval.
    """
    cfg = config or {}
    active_graph = graph or emaildrafter_graph
    parent_thread_id = (
        (cfg.get("configurable", {}).get("thread_id") if isinstance(cfg, dict) else None)
        or (Path(state.get("run_dir")).name if state.get("run_dir") else "run")
    )
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_draft"}}

    sub_input = {
        "messages": state.get("messages", []),
        "company_name": state.get("company_name"),
        "job_role": state.get("job_role"),
        "contact_email": state.get("contact_email"),
        "contact_name": state.get("contact_name"),
        "email_body": state.get("email_body"),
        "cover_letter": state.get("cover_letter"),
        "candidate_name": state.get("candidate_name"),
        "resume_path": state.get("resume_path"),
        "run_dir": state.get("run_dir"),
        "company_domain": state.get("company_domain"),
        "research_run_id": (cfg.get("configurable", {}).get("thread_id") if isinstance(cfg, dict) else None) or "run",
        "owner_id": state.get("owner_id") or app_config.LOCAL_OWNER_ID,
        "delivery_status": state.get("delivery_status"),
    }
    checkpoint = active_graph.get_state(clean_config)
    if checkpoint.next:
        result = checkpoint.values
    else:
        result = active_graph.invoke(sub_input, clean_config)

    while active_graph.get_state(clean_config).next:
        current_values = active_graph.get_state(clean_config).values
        print("\n--- READY TO SEND ---")
        print("To:", current_values.get("contact_email"))
        print("Subject:", current_values.get("subject"))
        print("Body:\n", current_values.get("email_body"))

        if decision_callback:
            decision = decision_callback(current_values)
        else:
            decision = input("Send this email now? (approve/reject): ").strip().lower()

        decision = str(decision or "").strip().lower()
        if decision == "pending":
            result = current_values
            break
        if decision not in {"approve", "reject"}:
            raise ValueError("Email decision must be approve, reject, or pending.")
        updates = {
            "human_decision": decision,
            "contact_email": state.get("contact_email"),
            "subject": state.get("subject") or current_values.get("subject"),
            "email_body": state.get("email_body"),
            "resume_path": state.get("resume_path"),
            "cover_letter": state.get("cover_letter"),
            "company_name": state.get("company_name"),
            "company_domain": state.get("company_domain"),
            "job_role": state.get("job_role"),
            "contact_name": state.get("contact_name"),
            "candidate_name": state.get("candidate_name"),
            "run_dir": state.get("run_dir"),
            "research_run_id": sub_input["research_run_id"],
            "owner_id": sub_input["owner_id"],
            "delivery_status": "approved" if decision == "approve" else "rejected",
        }
        active_graph.update_state(clean_config, updates)
        result = active_graph.invoke(None, clean_config)

    return {
        "subject": result.get("subject"),
        "final_email_sent": bool(result.get("sent", False)),
        "delivery_status": result.get("delivery_status") or ("draft_ready" if result.get("human_decision") in {None, "pending"} else "unknown"),
        "excel_updates_pending": [result.get("excel_log_entry")] if result.get("excel_log_entry") else None,
        "usage_summary": get_usage_summary(sub_input["research_run_id"]),
    }
