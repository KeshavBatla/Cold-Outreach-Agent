"""
Agents package for Cold Outreach Agent.
Exports agent execution functions and compiled graphs.
"""

from agents.manager import manager_graph, build_manager_graph, Manager, manager_router
from agents.researcher import (
    researcher_graph,
    build_researcher_graph,
    run_researcher,
)
from agents.cover_writer import (
    coverwriter_graph,
    build_coverwriter_graph,
    run_coverwriter,
)
from agents.email_writer import (
    emailwriter_graph,
    build_emailwriter_graph,
    run_emailwriter,
)
from agents.email_drafter import (
    emaildrafter_graph,
    build_emaildrafter_graph,
    run_emaildrafter,
)

__all__ = [
    "manager_graph",
    "build_manager_graph",
    "Manager",
    "manager_router",
    "researcher_graph",
    "build_researcher_graph",
    "run_researcher",
    "coverwriter_graph",
    "build_coverwriter_graph",
    "run_coverwriter",
    "emailwriter_graph",
    "build_emailwriter_graph",
    "run_emailwriter",
    "emaildrafter_graph",
    "build_emaildrafter_graph",
    "run_emaildrafter",
]

