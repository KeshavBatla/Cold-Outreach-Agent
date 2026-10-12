"""Agents package for Cold Outreach Agent."""

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


def __getattr__(name):
    if name in {"manager_graph", "build_manager_graph", "Manager", "manager_router"}:
        from agents.manager import manager_graph, build_manager_graph, Manager, manager_router
        return {"manager_graph": manager_graph, "build_manager_graph": build_manager_graph, "Manager": Manager, "manager_router": manager_router}[name]
    if name in {"researcher_graph", "build_researcher_graph", "run_researcher"}:
        from agents.researcher import researcher_graph, build_researcher_graph, run_researcher
        return {"researcher_graph": researcher_graph, "build_researcher_graph": build_researcher_graph, "run_researcher": run_researcher}[name]
    if name in {"coverwriter_graph", "build_coverwriter_graph", "run_coverwriter"}:
        from agents.cover_writer import coverwriter_graph, build_coverwriter_graph, run_coverwriter
        return {"coverwriter_graph": coverwriter_graph, "build_coverwriter_graph": build_coverwriter_graph, "run_coverwriter": run_coverwriter}[name]
    if name in {"emailwriter_graph", "build_emailwriter_graph", "run_emailwriter"}:
        from agents.email_writer import emailwriter_graph, build_emailwriter_graph, run_emailwriter
        return {"emailwriter_graph": emailwriter_graph, "build_emailwriter_graph": build_emailwriter_graph, "run_emailwriter": run_emailwriter}[name]
    if name in {"emaildrafter_graph", "build_emaildrafter_graph", "run_emaildrafter"}:
        from agents.email_drafter import emaildrafter_graph, build_emaildrafter_graph, run_emaildrafter
        return {"emaildrafter_graph": emaildrafter_graph, "build_emaildrafter_graph": build_emaildrafter_graph, "run_emaildrafter": run_emaildrafter}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

