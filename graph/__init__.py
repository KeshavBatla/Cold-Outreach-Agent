"""
Graph package for Cold Outreach Agent.
Exports state definitions and workflow graphs.
"""

from graph.state import (
    OutreachState,
    ResearcherState,
    CoverwriterState,
    EmailwriterState,
    EmaildrafterState,
)
from graph.workflow import (
    create_initial_outreach_state,
    run_outreach_cycle,
    manager_graph,
    build_manager_graph,
)

__all__ = [
    "OutreachState",
    "ResearcherState",
    "CoverwriterState",
    "EmailwriterState",
    "EmaildrafterState",
    "create_initial_outreach_state",
    "run_outreach_cycle",
    "manager_graph",
    "build_manager_graph",
]

