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

import config
from graph.state import EmaildrafterState, OutreachState
from prompts.email_writer import EMAIL_SUBJECT_SYSTEM_TEMPLATE
from evaluation.llm_evaluator import EmailSubject, get_subject_generator
from tools.pdf_generator import text_to_pdf
from tools.gmail import send_email


def emaildrafter(state: EmaildrafterState) -> Dict[str, Any]:
    company = state.get("company_name")
    role = state.get("job_role")
    if not company or not role:
        return {"subject": None}

    llm = config.get_llm()
    subject_generator = get_subject_generator(llm)

    result = subject_generator.invoke(
        {
            "messages": [
                SystemMessage(
                    content=EMAIL_SUBJECT_SYSTEM_TEMPLATE.format(role=role, company=company)
                ),
                HumanMessage(content="Generate it."),
            ]
        }
    )
    subject: EmailSubject = result["responses"][0]
    return {"subject": subject.subject}


def human_evaluation_draft(state: EmaildrafterState) -> Dict[str, Any]:
    """Human-in-the-loop checkpoint before actually sending email."""
    return {}


def human_router_draft(state: EmaildrafterState) -> str:
    return "emailsender" if state.get("human_decision") == "approve" else "emaildrafter"


def emailsender(state: EmaildrafterState) -> Dict[str, Any]:
    to_email = state.get("contact_email")
    company_name = state.get("company_name", "Target Company")
    role = state.get("job_role", "Target Role")
    subject = state.get("subject") or f"Application for {role} at {company_name}"
    body = state.get("email_body") or ""

    if not to_email:
        return {
            "sent": False,
            "excel_log_entry": {
                "company": company_name,
                "status": "failed - no contact email",
            },
        }

    attachments = []

    # 1. Attach Resume / CV
    resume_path = state.get("resume_path") or config.DEFAULT_RESUME_PATH
    if resume_path and Path(resume_path).exists():
        attachments.append(resume_path)
    else:
        print(f"[emailsender] WARNING: Resume not found at {resume_path} — proceeding without it.")

    # 2. Render and attach Cover Letter PDF
    cover_letter_text = state.get("cover_letter")
    if cover_letter_text:
        clean_company = company_name.replace(" ", "_").replace("/", "_")
        pdf_path = Path(config.COVER_LETTER_DIR) / f"cover_letter_{clean_company}.pdf"
        try:
            text_to_pdf(cover_letter_text, str(pdf_path), title=f"Cover Letter — {company_name}")
            attachments.append(str(pdf_path))
        except Exception as e:
            print(f"[emailsender] Error generating cover letter PDF: {e}")
    else:
        print("[emailsender] WARNING: No cover letter found — proceeding without it.")

    # 3. Send message via Gmail tool
    send_result = send_email(
        to_email=to_email,
        subject=subject,
        body=body,
        attachment_paths=attachments,
        dry_run=config.DRY_RUN,
    )

    if send_result.get("sent"):
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": "sent",
            "gmail_message_id": send_result.get("message_id"),
        }
        return {"sent": True, "excel_log_entry": log_entry}
    elif send_result.get("dry_run"):
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": "dry-run",
            "gmail_message_id": "dry-run-preview",
        }
        return {"sent": False, "excel_log_entry": log_entry}
    else:
        log_entry = {
            "company": company_name,
            "contact_email": to_email,
            "role": role,
            "status": f"failed - {send_result.get('error')}",
        }
        return {"sent": False, "excel_log_entry": log_entry}


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


def run_emaildrafter(
    state: OutreachState,
    cfg: Dict[str, Any],
    decision_callback: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    """
    Execute the Emaildrafter subgraph and handle human approval.
    """
    parent_thread_id = cfg["configurable"]["thread_id"]
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_draft"}}

    sub_input = {
        "messages": state.get("messages", []),
        "company_name": state.get("company_name"),
        "job_role": state.get("job_role"),
        "contact_email": state.get("contact_email"),
        "contact_name": state.get("contact_name"),
        "email_body": state.get("email_body"),
        "cover_letter": state.get("cover_letter"),
        "resume_path": state.get("resume_path") or config.DEFAULT_RESUME_PATH,
    }
    result = emaildrafter_graph.invoke(sub_input, clean_config)

    while emaildrafter_graph.get_state(clean_config).next:
        current_values = emaildrafter_graph.get_state(clean_config).values
        print("\n--- READY TO SEND ---")
        print("To:", current_values.get("contact_email"))
        print("Subject:", current_values.get("subject"))
        print("Body:\n", current_values.get("email_body"))

        if decision_callback:
            decision = decision_callback(current_values)
        else:
            decision = input("Send this email now? (approve/reject): ").strip().lower()

        emaildrafter_graph.update_state(clean_config, {"human_decision": decision})
        result = emaildrafter_graph.invoke(None, clean_config)

    return {
        "subject": result.get("subject"),
        "final_email_sent": result.get("sent", False),
        "excel_updates_pending": [result.get("excel_log_entry")] if result.get("excel_log_entry") else None,
    }

