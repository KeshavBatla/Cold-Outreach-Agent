"""
Coverwriter Agent module.
Generates tailored, punchy cover letters based on candidate CV and company research,
with an automated LLM evaluation and refinement feedback loop.
"""

from typing import Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config
from graph.state import CoverwriterState, OutreachState
from prompts.candidate import CANDIDATE_CV, EXAMPLE_COVER_LETTER
from prompts.cover_writer import (
    COVER_LETTER_SYSTEM_TEMPLATE,
    COVER_LETTER_HUMAN_PROMPT,
    COVER_LETTER_EVALUATION_TEMPLATE,
)
from evaluation.llm_evaluator import CoverLetterEvaluation, get_coverletter_evaluator
from utils.helpers import extract_text


def coverletterwrite(state: CoverwriterState) -> Dict[str, Any]:
    company = state.get("company_name", "")
    role = state.get("job_role", "")
    print(f"[coverletterwrite] Attempting generation — retry_count={state.get('retry_count', 0)}, company={company!r}, role={role!r}")

    if not company or not role:
        return {
            "cover_letter": None,
            "evaluator_feedback": "Missing company_name or job_role input — cannot write a targeted letter.",
            "letter_sufficient": False,
        }

    description = state.get("job_description") or "Not specified"
    research = state.get("research_summary") or ""
    feedback = state.get("evaluator_feedback")

    system_text = COVER_LETTER_SYSTEM_TEMPLATE.format(
        candidate_cv=CANDIDATE_CV,
        example_cover_letter=EXAMPLE_COVER_LETTER,
        company=company,
        role=role,
        description=description,
        research=research,
    )

    if feedback:
        system_text += f"\nPrevious draft's feedback (fix this): {feedback}\n"

    llm = config.get_llm()
    response = llm.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=COVER_LETTER_HUMAN_PROMPT),
        ]
    )
    return {"cover_letter": extract_text(response.content)}


def llm_evaluator_cover(state: CoverwriterState) -> Dict[str, Any]:
    cover_letter = extract_text(state.get("cover_letter", ""))
    current_retry = state.get("retry_count", 0)
    print(f"[llm_evaluator_cover] Evaluating draft — retry_count={current_retry}, letter_present={bool(cover_letter)}")

    if not cover_letter:
        return {
            "evaluator_feedback": "No cover letter was generated.",
            "letter_sufficient": False,
            "retry_count": current_retry + 1,
        }

    llm = config.get_llm()
    evaluator = get_coverletter_evaluator(llm)

    messages = [
        SystemMessage(
            content=COVER_LETTER_EVALUATION_TEMPLATE.format(
                company=state.get("company_name"), role=state.get("job_role")
            )
        ),
        HumanMessage(content=cover_letter),
    ]
    result = evaluator.invoke({"messages": messages})
    evaluation: CoverLetterEvaluation = result["responses"][0]
    print(f"[llm_evaluator_cover] is_sufficient={evaluation.is_sufficient}, feedback={evaluation.feedback[:100]}")

    updates = {
        "evaluator_feedback": evaluation.feedback,
        "letter_sufficient": evaluation.is_sufficient,
    }
    if not evaluation.is_sufficient:
        updates["retry_count"] = current_retry + 1
    return updates


def llm_router_cover(state: CoverwriterState) -> str:
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries") or config.MAX_RETRIES
    print(f"[llm_router_cover] retry_count={retry_count}, max_retries={max_retries}, letter_sufficient={state.get('letter_sufficient')}")

    if state.get("letter_sufficient") is True:
        return END
    if retry_count >= max_retries:
        return END
    return "coverletterwrite"


def build_coverwriter_graph(checkpointer=None):
    """
    Build and compile the Coverwriter Subgraph.
    """
    builder = StateGraph(CoverwriterState)
    builder.add_node("coverletterwrite", coverletterwrite)
    builder.add_node("llm_evaluator", llm_evaluator_cover)
    builder.add_edge(START, "coverletterwrite")
    builder.add_edge("coverletterwrite", "llm_evaluator")
    builder.add_conditional_edges("llm_evaluator", llm_router_cover, ["coverletterwrite", END])

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=cp)


coverwriter_graph = build_coverwriter_graph()


def run_coverwriter(state: OutreachState, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute the Coverwriter subgraph.
    """
    parent_thread_id = cfg["configurable"]["thread_id"]
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_cover"}}

    sub_input = {
        "messages": state.get("messages", []),
        "company_name": state.get("company_name"),
        "job_role": state.get("job_role"),
        "job_description": state.get("job_description"),
        "research_summary": state.get("research_summary"),
        "retry_count": 0,
        "max_retries": state.get("max_retries", config.MAX_RETRIES),
    }
    result = coverwriter_graph.invoke(sub_input, clean_config)
    print("\n--- COVER LETTER DRAFT ---")
    print(result.get("cover_letter"))
    print("--- EVALUATOR FEEDBACK ---")
    print(result.get("evaluator_feedback"))

    return {
        "cover_letter": result.get("cover_letter"),
        "cover_letter_approved": bool(result.get("letter_sufficient")),
        "cover_letter_attempts": state.get("cover_letter_attempts", 0) + 1,
    }

