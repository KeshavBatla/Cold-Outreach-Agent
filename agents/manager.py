"""
Supervisor / Manager Agent module.
Coordinates the multi-agent outreach lifecycle: Researcher -> Coverwriter -> Emailwriter -> Emaildrafter.
"""

from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

import config
from graph.state import OutreachState
from agents.researcher import run_researcher
from agents.cover_writer import run_coverwriter
from agents.email_writer import run_emailwriter
from agents.email_drafter import run_emaildrafter


def Manager(state: OutreachState) -> Dict[str, Any]:
    """
    Pure pass-through supervisor node. Routing decisions are handled by manager_router.
    """
    return {}


def manager_router(state: OutreachState) -> str:
    """
    Route to the next agent based on completion and approval flags.
    """
    if not state.get("research_approved"):
        return "Researcher"
    if not state.get("cover_letter_approved"):
        if state.get("cover_letter_attempts", 0) >= config.MAX_STAGE_ATTEMPTS:
            print("[Manager] Max cover letter attempts reached without approval. Halting.")
            return END
        return "Coverwriter"
    if not state.get("email_approved"):
        if state.get("email_attempts", 0) >= config.MAX_STAGE_ATTEMPTS:
            print("[Manager] Max email attempts reached without approval. Halting.")
            return END
        return "Emailwriter"
    if not state.get("final_email_sent"):
        return "Emaildrafter"
    return END


def build_manager_graph(checkpointer=None):
    """
    Construct and compile the Master Supervisor Outreach Graph.
    """
    builder = StateGraph(OutreachState)

    builder.add_node("Manager", Manager)
    builder.add_node("Researcher", run_researcher)
    builder.add_node("Coverwriter", run_coverwriter)
    builder.add_node("Emailwriter", run_emailwriter)
    builder.add_node("Emaildrafter", run_emaildrafter)

    builder.add_edge(START, "Manager")
    builder.add_conditional_edges(
        "Manager",
        manager_router,
        ["Researcher", "Coverwriter", "Emailwriter", "Emaildrafter", END],
    )
    builder.add_edge("Researcher", "Manager")
    builder.add_edge("Coverwriter", "Manager")
    builder.add_edge("Emailwriter", "Manager")
    builder.add_edge("Emaildrafter", "Manager")

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=cp)


manager_graph = build_manager_graph()

