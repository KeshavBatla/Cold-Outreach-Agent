"""
Streamlit Web Application for Cold Outreach Multi-Agent System.
Provides an interactive dashboard for initiating outreach runs, inspecting agent outputs,
reviewing evaluation feedback, and providing human approval at interrupt points.
"""

import sys
from pathlib import Path
import uuid

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
import config
from utils.cv_parser import build_run_inputs
from graph.workflow import create_initial_outreach_state
from agents.researcher import run_researcher
from agents.cover_writer import run_coverwriter
from agents.email_writer import run_emailwriter
from agents.email_drafter import run_emaildrafter

st.set_page_config(
    page_title="Cold Outreach Multi-Agent System",
    page_icon="🚀",
    layout="wide",
)

st.title("🚀 Cold Outreach Multi-Agent System")
st.caption("Orchestrated with LangGraph, Google Gemini, Tavily Search, Hunter.io & Gmail API")

# Initialize session state for workflow lifecycle
if "outreach_state" not in st.session_state:
    st.session_state.outreach_state = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"st_run_{uuid.uuid4().hex[:8]}"
if "stage" not in st.session_state:
    st.session_state.stage = "idle"  # idle -> researched -> cover_drafted -> email_drafted -> ready_to_send -> completed
if "run_inputs" not in st.session_state:
    st.session_state.run_inputs = None
if "candidate_profile" not in st.session_state:
    st.session_state.candidate_profile = ""
if "uploaded_cv_name" not in st.session_state:
    st.session_state.uploaded_cv_name = None

# Sidebar configuration
with st.sidebar:
    st.header("⚙️ System Configuration")

    # API Keys Status
    st.subheader("Integrations Status")
    col1, col2 = st.columns(2)
    with col1:
        st.write("Gemini:")
        st.write("Tavily:")
        st.write("Hunter:")
    with col2:
        st.write("✅ Active" if config.GEMINI_API_KEY else "❌ Missing")
        st.write("✅ Active" if config.TAVILY_API_KEY else "❌ Missing")
        st.write("✅ Active" if config.HUNTER_API_KEY else "⚠️ Optional")

    dry_run_toggle = st.toggle("Dry Run (Simulate Send)", value=config.DRY_RUN)
    config.DRY_RUN = dry_run_toggle

    st.markdown("---")
    st.subheader("Targeting Strategy")
    mode_option = st.radio(
        "Discovery Mode",
        ["Target Named Company (open_role)", "Speculative Opportunity Discovery (speculative)"],
    )
    is_open_role = "Target Named Company" in mode_option

    company_input = ""
    if is_open_role:
        company_input = st.text_input(
            "Company Name",
            placeholder="e.g. Simular, Perplexity AI, Cohere",
        )

    target_seniority = st.text_input(
        "Target Seniority",
        placeholder="e.g. Intern, Junior, Fresher",
    )
    target_domain_keywords = st.text_input(
        "Target Domain Keywords",
        placeholder="e.g. Agentic AI, LLM systems, Machine Learning",
    )
    location_preference = st.text_input(
        "Location Preference",
        placeholder="e.g. Remote, India, US",
    )

    st.markdown("---")
    st.subheader("Candidate Resume & Documents")
    cv_file = st.file_uploader("Upload CV (PDF or DOCX)", type=["pdf", "docx"])
    example_letter_file = st.file_uploader("Upload Example Letter (Optional)", type=["pdf", "docx", "txt"])

    if cv_file is not None and st.session_state.uploaded_cv_name != cv_file.name:
        run_dir = config.get_run_dir(st.session_state.thread_id)
        cv_dest = run_dir / cv_file.name
        with open(cv_dest, "wb") as f:
            f.write(cv_file.getbuffer())

        example_dest = None
        if example_letter_file is not None:
            example_dest = run_dir / example_letter_file.name
            with open(example_dest, "wb") as f:
                f.write(example_letter_file.getbuffer())

        try:
            parsed_inputs = build_run_inputs(
                cv_path=cv_dest,
                example_letter_path=example_dest,
                run_dir=run_dir,
            )
            st.session_state.run_inputs = parsed_inputs
            st.session_state.candidate_profile = parsed_inputs.candidate_profile
            st.session_state.uploaded_cv_name = cv_file.name
            st.success(f"Loaded profile for: {parsed_inputs.candidate_name}")
        except Exception as e:
            st.error(f"Error parsing CV: {e}")

    if st.button("Reset Workflow", use_container_width=True):
        st.session_state.outreach_state = None
        st.session_state.thread_id = f"st_run_{uuid.uuid4().hex[:8]}"
        st.session_state.stage = "idle"
        st.session_state.run_inputs = None
        st.session_state.candidate_profile = ""
        st.session_state.uploaded_cv_name = None
        st.rerun()

# Workflow Pipeline View
tab_pipeline, tab_profile, tab_logs = st.tabs(["⚡ Outreach Pipeline", "👤 Candidate Profile", "📜 System Logs"])

with tab_profile:
    st.subheader("Candidate Profile & Skills")
    profile_text = st.text_area(
        "Active Candidate Profile (used by Researcher and Writers)",
        value=st.session_state.candidate_profile,
        height=200,
    )
    st.session_state.candidate_profile = profile_text

with tab_logs:
    st.subheader("Execution State Log")
    if st.session_state.outreach_state:
        st.json(st.session_state.outreach_state)
    else:
        st.info("No active outreach run state. Start a cycle in the pipeline tab.")

with tab_pipeline:
    # 0. Start Stage
    if st.session_state.stage == "idle":
        st.info("Upload your CV in the sidebar and trigger the agent workflow below.")
        if st.button("🚀 Start Outreach Cycle", type="primary", use_container_width=True):
            if not st.session_state.run_inputs:
                st.error("Please upload a CV (PDF or DOCX) in the sidebar before starting.")
            else:
                inputs = st.session_state.run_inputs
                resolved_mode = "open_role" if (is_open_role and company_input.strip()) else "speculative"
                seed = company_input.strip() if is_open_role else None
                run_dir = config.get_run_dir(st.session_state.thread_id)

                st.session_state.outreach_state = create_initial_outreach_state(
                    company_name=seed,
                    mode=resolved_mode,
                    candidate_profile=st.session_state.candidate_profile or inputs.candidate_profile,
                    resume_path=inputs.resume_path,
                    raw_cv_text=inputs.raw_cv_text,
                    example_cover_letter=inputs.example_cover_letter,
                    target_seniority=target_seniority or None,
                    target_domain_keywords=target_domain_keywords or None,
                    location_preference=location_preference or None,
                    candidate_name=inputs.candidate_name,
                    run_dir=str(run_dir),
                )
                st.session_state.stage = "running_researcher"
                st.rerun()

    # 1. Running Researcher Agent
    if st.session_state.stage == "running_researcher":
        with st.spinner("🔍 Researcher Agent is scanning the web, extracting job roles, and discovering verified contacts..."):
            cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
            def ui_approve(_):
                return "approve"

            res = run_researcher(st.session_state.outreach_state, cfg, decision_callback=ui_approve)
            st.session_state.outreach_state.update(res)
            st.session_state.stage = "research_review"
            st.rerun()

    # 2. Human-In-The-Loop: Research Review
    if st.session_state.stage == "research_review":
        st.success("✅ Research Phase Complete — Human Review Required")
        state = st.session_state.outreach_state

        col_left, col_right = st.columns([1, 1])
        with col_left:
            st.markdown(f"**Target Company:** `{state.get('company_name')}`")
            st.markdown(f"**Target Role:** `{state.get('job_role')}`")
            st.markdown(f"**Contact:** `{state.get('contact_name')}` (`{state.get('contact_email')}`)")
        with col_right:
            st.markdown(f"**Job Summary:**\n{state.get('job_description') or 'N/A'}")
            st.markdown(f"**Research Insights:**\n{state.get('research_summary') or 'N/A'}")

        st.markdown("---")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("👍 Approve Research & Generate Cover Letter", type="primary", use_container_width=True):
                st.session_state.outreach_state["research_approved"] = True
                st.session_state.stage = "running_coverwriter"
                st.rerun()
        with c2:
            if st.button("🔄 Reject & Re-run Research", use_container_width=True):
                st.session_state.outreach_state["research_approved"] = False
                st.session_state.stage = "running_researcher"
                st.rerun()

    # 3. Running Coverwriter Agent
    if st.session_state.stage == "running_coverwriter":
        with st.spinner("✍️ Coverwriter Agent is generating tailored cover letter and running LLM evaluator..."):
            cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
            res = run_coverwriter(st.session_state.outreach_state, cfg)
            st.session_state.outreach_state.update(res)
            st.session_state.stage = "cover_review"
            st.rerun()

    # 4. Review Cover Letter
    if st.session_state.stage == "cover_review":
        st.success("✅ Cover Letter Drafted & Evaluated")
        state = st.session_state.outreach_state

        st.text_area("Generated Cover Letter", value=state.get("cover_letter", ""), height=350)

        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("👍 Approve Cover Letter & Draft Email", type="primary", use_container_width=True):
                st.session_state.outreach_state["cover_letter_approved"] = True
                st.session_state.stage = "running_emailwriter"
                st.rerun()
        with c2:
            if st.button("🔄 Regenerate Cover Letter", use_container_width=True):
                st.session_state.outreach_state["cover_letter_approved"] = False
                st.session_state.stage = "running_coverwriter"
                st.rerun()

    # 5. Running Emailwriter Agent
    if st.session_state.stage == "running_emailwriter":
        with st.spinner("📧 Emailwriter Agent is creating personalized cold email body..."):
            cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
            res = run_emailwriter(st.session_state.outreach_state, cfg)
            st.session_state.outreach_state.update(res)
            st.session_state.stage = "running_drafter"
            st.rerun()

    # 6. Running Emaildrafter (Subject line generation)
    if st.session_state.stage == "running_drafter":
        with st.spinner("📑 Emaildrafter Agent is generating subject line and packaging attachments..."):
            cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
            def wait_for_ui_send(_):
                return "reject"  # Pause before sending

            res = run_emaildrafter(st.session_state.outreach_state, cfg, decision_callback=wait_for_ui_send)
            st.session_state.outreach_state.update(res)
            st.session_state.stage = "final_review"
            st.rerun()

    # 7. Final Human Review & Dispatch
    if st.session_state.stage == "final_review":
        st.warning("⚠️ Ready for Final Human Review Before Transmission")
        state = st.session_state.outreach_state

        st.markdown(f"**To:** `{state.get('contact_email')}` ({state.get('contact_name')})")
        st.markdown(f"**Company:** `{state.get('company_name')}`")
        subject = st.text_input("Subject Line", value=state.get("subject") or f"Application for {state.get('job_role')} at {state.get('company_name')}")
        st.session_state.outreach_state["subject"] = subject

        email_body = st.text_area("Email Body", value=state.get("email_body", ""), height=150)
        st.session_state.outreach_state["email_body"] = email_body

        st.markdown("**Attachments:**")
        st.write(f"1. Resume: `{state.get('resume_path')}`")
        clean_company = (state.get("company_name") or "Company").replace(" ", "_").replace("/", "_")
        run_dir = state.get("run_dir")
        pdf_path = (Path(run_dir) / f"cover_letter_{clean_company}.pdf") if run_dir else f"cover_letter_{clean_company}.pdf"
        st.write(f"2. Formatted PDF Cover Letter: `{pdf_path}`")

        c1, c2 = st.columns([1, 1])
        with c1:
            send_btn_label = "🧪 Dispatch (Dry Run)" if config.DRY_RUN else "📤 Send Email Now via Gmail API"
            if st.button(send_btn_label, type="primary", use_container_width=True):
                cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
                def confirm_send(_):
                    return "approve"
                res = run_emaildrafter(st.session_state.outreach_state, cfg, decision_callback=confirm_send)
                st.session_state.outreach_state.update(res)
                st.session_state.stage = "completed"
                st.rerun()
        with c2:
            if st.button("❌ Abort Outreach", use_container_width=True):
                st.session_state.stage = "idle"
                st.session_state.outreach_state = None
                st.rerun()

    # 8. Completed
    if st.session_state.stage == "completed":
        st.balloons()
        state = st.session_state.outreach_state
        if state.get("final_email_sent"):
            st.success("🎉 Outreach email successfully sent via Gmail API!")
        else:
            st.info("ℹ️ Outreach completed in Dry Run mode (or transmission was skipped).")

        st.json(state.get("excel_updates_pending"))

        if st.button("Start Another Run", type="primary"):
            st.session_state.outreach_state = None
            st.session_state.thread_id = f"st_run_{uuid.uuid4().hex[:8]}"
            st.session_state.stage = "idle"
            st.session_state.run_inputs = None
            st.session_state.candidate_profile = ""
            st.session_state.uploaded_cv_name = None
            st.rerun()
