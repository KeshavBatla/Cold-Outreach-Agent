"""
Tests for build_run_inputs, run isolation, and cover letter writer validations.
"""

import os
from pathlib import Path
import pytest
import config
from tools.pdf_generator import text_to_pdf
from utils.cv_parser import build_run_inputs, extract_candidate_name, generate_candidate_profile
from graph.workflow import create_initial_outreach_state
from agents.cover_writer import coverletterwrite

def create_dummy_pdf(path: Path, text: str):
    text_to_pdf(text, str(path))


def test_create_initial_outreach_state_missing_resume_raises_value_error():
    with pytest.raises(ValueError, match="resume_path is required"):
        create_initial_outreach_state(resume_path=None)

    with pytest.raises(ValueError, match="resume_path is required"):
        create_initial_outreach_state(resume_path="")


def test_get_run_dir_creates_isolated_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    run_dir = config.get_run_dir("test_thread_123")
    assert run_dir.exists()
    assert run_dir.is_dir()
    assert run_dir.name == "test_thread_123"


def test_build_run_inputs_invalid_file_type(tmp_path):
    invalid_file = tmp_path / "resume.txt"
    invalid_file.write_text("Hello world")
    with pytest.raises(ValueError, match="Only .pdf and .docx files are supported"):
        build_run_inputs(invalid_file)


def test_build_run_inputs_short_text_raises_error(tmp_path):
    short_pdf = tmp_path / "short_resume.pdf"
    create_dummy_pdf(short_pdf, "Short CV with only twenty characters.")
    with pytest.raises(ValueError, match="empty or under 200 characters"):
        build_run_inputs(short_pdf)


def test_build_run_inputs_valid_pdf_and_isolation(tmp_path):
    cv_pdf = tmp_path / "candidate_alice.pdf"
    content = (
        "Alice Smith\n"
        "alice@example.com | +1 555-0199 | San Francisco, CA\n\n"
        "Professional Summary:\n"
        "Senior Software Engineer with 6 years of experience building distributed systems, "
        "microservices, and modern cloud applications using Python, Go, and Kubernetes.\n\n"
        "Experience:\n"
        "Staff Engineer at Acme Corp (2021-Present)\n"
        "- Architected high-throughput message streaming pipeline handling 50k events per second.\n"
        "- Mentored junior engineers and led system architecture reviews across 4 agile teams.\n\n"
        "Education:\n"
        "B.S. in Computer Science, University of California, Berkeley (2015-2019).\n"
    )
    create_dummy_pdf(cv_pdf, content)

    run_dir = tmp_path / "runs" / "run_alice"
    inputs = build_run_inputs(cv_path=cv_pdf, run_dir=run_dir)

    # Check 5-tuple unpacking
    raw_cv, profile, name, res_path, ex_letter = inputs
    assert "Alice Smith" in raw_cv
    assert name == "Alice Smith"
    assert ex_letter is None
    assert Path(res_path).parent == run_dir
    assert Path(res_path).name == "candidate_alice.pdf"
    assert Path(res_path).exists()


def test_cover_writer_empty_cv_raises_value_error():
    state = {
        "company_name": "TestCorp",
        "job_role": "Engineer",
        "raw_cv_text": "",
    }
    with pytest.raises(ValueError, match="raw_cv_text is empty"):
        coverletterwrite(state)
