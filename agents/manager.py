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
from agents.researcher import build_researcher_graph
from agents.cover_writer import build_coverwriter_graph
from agents.email_writer import build_emailwriter_graph
from agents.email_drafter import build_emaildrafter_graph


def Manager(state: OutreachState) -> Dict[str, Any]:
    """
    Pure pass-through supervisor node. Routing decisions are handled by manager_router.
    """
    return {}


def manager_router(state: OutreachState) -> str:
    """
    Route to the next agent based on completion and approval flags.
    """
    if state.get("research_outcome") and not state.get("research_approved"):
        print(
            f"[Manager] Research outcome is {state.get('research_outcome')!r} without explicit approval; stopping."
        )
        return END
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


def build_manager_graph(checkpointer=None, subgraph_checkpointer=None):
    """
    Construct and compile the Master Supervisor Outreach Graph.
    """
    builder = StateGraph(OutreachState)
    active_subgraph_checkpointer = subgraph_checkpointer or checkpointer
    if active_subgraph_checkpointer is None:
        nodes = {
            "Researcher": run_researcher,
            "Coverwriter": run_coverwriter,
            "Emailwriter": run_emailwriter,
            "Emaildrafter": run_emaildrafter,
        }
    else:
        research_graph = build_researcher_graph(checkpointer=active_subgraph_checkpointer)
        cover_graph = build_coverwriter_graph(checkpointer=active_subgraph_checkpointer)
        email_graph = build_emailwriter_graph(checkpointer=active_subgraph_checkpointer)
        drafter_graph = build_emaildrafter_graph(checkpointer=active_subgraph_checkpointer)
        nodes = {
            "Researcher": lambda state, config: run_researcher(state, config, graph=research_graph),
            "Coverwriter": lambda state, config: run_coverwriter(state, config, graph=cover_graph),
            "Emailwriter": lambda state, config: run_emailwriter(state, config, graph=email_graph),
            "Emaildrafter": lambda state, config: run_emaildrafter(state, config, graph=drafter_graph),
        }

    builder.add_node("Manager", Manager)
    for name, node in nodes.items():
        builder.add_node(name, node)

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
