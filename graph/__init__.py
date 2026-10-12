"""Graph package for Cold Outreach Agent."""

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


def __getattr__(name):
    if name in {"OutreachState", "ResearcherState", "CoverwriterState", "EmailwriterState", "EmaildrafterState"}:
        from graph.state import OutreachState, ResearcherState, CoverwriterState, EmailwriterState, EmaildrafterState
        return {
            "OutreachState": OutreachState,
            "ResearcherState": ResearcherState,
            "CoverwriterState": CoverwriterState,
            "EmailwriterState": EmailwriterState,
            "EmaildrafterState": EmaildrafterState,
        }[name]
    if name in {"create_initial_outreach_state", "run_outreach_cycle", "manager_graph", "build_manager_graph"}:
        from graph.workflow import create_initial_outreach_state, run_outreach_cycle, manager_graph, build_manager_graph
        return {
            "create_initial_outreach_state": create_initial_outreach_state,
            "run_outreach_cycle": run_outreach_cycle,
            "manager_graph": manager_graph,
            "build_manager_graph": build_manager_graph,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

