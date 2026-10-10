"""
CLI Programmatic Entry Point for Cold Outreach Multi-Agent System.
"""

import sys
import uuid
import argparse
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from utils.cv_parser import build_run_inputs
from graph.workflow import run_outreach_cycle
from agents.manager import manager_graph


def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Cold Outreach Agent powered by LangGraph & Gemini"
    )
    parser.add_argument(
        "-c",
        "--company",
        type=str,
        default=None,
        help="Target company name (e.g. 'Simular', 'Cohere'). If omitted, runs speculative discovery.",
    )
    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        choices=["open_role", "speculative"],
        default=None,
        help="Search mode: 'open_role' targets specific company job postings; 'speculative' finds tech companies hiring interns.",
    )
    parser.add_argument(
        "--cv",
        type=str,
        default=None,
        help="Path to candidate CV file (PDF or DOCX). Required for outreach runs.",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Alias for --cv.",
    )
    parser.add_argument(
        "--example-letter",
        type=str,
        default=None,
        help="Optional path to reference cover letter file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without actually sending emails via Gmail (simulates dispatch).",
    )
    parser.add_argument(
        "--inspect-graph",
        action="store_true",
        help="Print Mermaid diagram representation of the multi-agent graph and exit.",
    )

    args = parser.parse_args()

    if args.inspect_graph:
        print("\n--- MASTER SUPERVISOR MERMAID GRAPH ---")
        try:
            print(manager_graph.get_graph(xray=1).draw_mermaid())
        except Exception as e:
            print(f"Mermaid drawing requires grandalf or pygraphviz: {e}")
        return

    if args.dry_run:
        config.DRY_RUN = True
        print("[Notice] Running in DRY_RUN mode. Emails will NOT be dispatched.")

    cv_input = args.cv or args.resume
    if not cv_input:
        parser.error("Missing required argument: --cv (or --resume)")

    try:
        thread_id = f"cli_run_{uuid.uuid4().hex[:8]}"
        run_dir = config.get_run_dir(thread_id)

        inputs = build_run_inputs(
            cv_path=cv_input,
            example_letter_path=args.example_letter,
            run_dir=run_dir,
        )

        run_outreach_cycle(
            company_name=args.company,
            mode=args.mode,
            thread_id=thread_id,
            candidate_profile=inputs.candidate_profile,
            resume_path=inputs.resume_path,
            raw_cv_text=inputs.raw_cv_text,
            example_cover_letter=inputs.example_cover_letter,
            candidate_name=inputs.candidate_name,
            run_dir=str(run_dir),
        )
    except KeyboardInterrupt:
        print("\n[Outreach Pipeline] Execution aborted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Outreach Pipeline Error]: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
