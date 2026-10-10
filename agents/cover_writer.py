"""
Coverwriter Agent module.
Generates tailored, punchy cover letters based on candidate CV and company research,
with an automated LLM evaluation and refinement feedback loop.
"""

from typing import Dict, Any, Optional
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config as app_config
from graph.state import CoverwriterState, OutreachState
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
    candidate_cv = state.get("raw_cv_text", "")
    if not candidate_cv or not candidate_cv.strip():
        raise ValueError("raw_cv_text is empty. Cannot write a cover letter without a CV.")

    example_cover_letter = state.get("example_cover_letter", "")

    # Build the example section if example cover letter is provided
    if example_cover_letter and example_cover_letter.strip():
        example_section = (
            "Here is an example of a strong cover letter this candidate has written before. "
            "Match its tone and structure only, never copy facts, names or projects from it:\n"
            f"{example_cover_letter.strip()}\n\n"
        )
    else:
        example_section = ""

    system_text = COVER_LETTER_SYSTEM_TEMPLATE.format(
        candidate_cv=candidate_cv,
        example_section=example_section,
        company=company,
        role=role,
        description=description,
        research=research,
    )

    if feedback:
        system_text += f"\nPrevious draft's feedback (fix this): {feedback}\n"

    llm = app_config.get_llm()
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

    llm = app_config.get_llm()
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
    max_retries = state.get("max_retries") or app_config.MAX_RETRIES
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


from langchain_core.runnables import RunnableConfig


def run_coverwriter(state: OutreachState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Execute the Coverwriter subgraph.
    """
    cfg = config or {}
    parent_thread_id = (
        (cfg.get("configurable", {}).get("thread_id") if isinstance(cfg, dict) else None)
        or (Path(state.get("run_dir")).name if state.get("run_dir") else "run")
    )
    clean_config = {"configurable": {"thread_id": f"{parent_thread_id}_cover"}}

    sub_input = {
        "messages": state.get("messages", []),
        "company_name": state.get("company_name"),
        "job_role": state.get("job_role"),
        "job_description": state.get("job_description"),
        "research_summary": state.get("research_summary"),
        "raw_cv_text": state.get("raw_cv_text"),
        "example_cover_letter": state.get("example_cover_letter"),
        "retry_count": 0,
        "max_retries": state.get("max_retries", app_config.MAX_RETRIES),
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

