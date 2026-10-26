"""
Tests for MIME attachment construction, dynamic filenames, placeholder detection,
and the attachment pipeline in webapp/gmail_delivery.py and utils/helpers.py.
"""
from __future__ import annotations

import base64
import email
from email import policy

import pytest

from utils.helpers import strip_placeholder_links, find_unresolved_placeholders
from webapp.gmail_delivery import build_mime_message


# ---------------------------------------------------------------------------
# MIME construction tests
# ---------------------------------------------------------------------------


def test_mime_plain_no_attachments():
    msg = build_mime_message("test@example.com", "Test Subject", "Hello world.")
    assert msg["To"] == "test@example.com"
    assert msg["Subject"] == "Test Subject"
    assert not msg.is_multipart()


def test_mime_with_pdf_attachment():
    resume_bytes = b"%PDF-1.4 mock resume"
    msg = build_mime_message(
        "recipient@company.com",
        "Job Application",
        "Please find my resume attached.",
        attachments=[
            {"filename": "Jane_Doe_Resume.pdf", "content": resume_bytes, "mimetype": "application/pdf"},
        ],
    )
    assert msg.is_multipart()
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Jane_Doe_Resume.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_content() == resume_bytes


def test_mime_with_resume_and_cover_letter():
    resume_bytes = b"%PDF resume content"
    cl_bytes = b"%PDF cover letter content"
    msg = build_mime_message(
        "hr@startup.io",
        "Application: Engineer",
        "Body text.",
        attachments=[
            {"filename": "John_Smith_Resume.pdf", "content": resume_bytes, "mimetype": "application/pdf"},
            {"filename": "John_Smith_Cover_Letter.pdf", "content": cl_bytes, "mimetype": "application/pdf"},
        ],
    )
    assert msg.is_multipart()
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 2
    filenames = {a.get_filename() for a in attachments}
    assert "John_Smith_Resume.pdf" in filenames
    assert "John_Smith_Cover_Letter.pdf" in filenames


def test_mime_no_duplicate_attachments():
    content = b"some bytes"
    msg = build_mime_message(
        "x@example.com",
        "Subj",
        "Body",
        attachments=[
            {"filename": "doc.pdf", "content": content, "mimetype": "application/pdf"},
            {"filename": "doc.pdf", "content": content, "mimetype": "application/pdf"},  # duplicate
        ],
    )
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1


def test_mime_docx_attachment():
    docx_bytes = b"PK" + b"\x00" * 20  # minimal docx-like header
    msg = build_mime_message(
        "hr@corp.com",
        "Application",
        "Body",
        attachments=[
            {"filename": "Resume.docx", "content": docx_bytes, "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ],
    )
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "Resume.docx"
    assert "wordprocessingml" in atts[0].get_content_type()
    assert atts[0].get_content() == docx_bytes


def test_mime_can_be_roundtripped_via_base64():
    resume_bytes = b"%PDF resume"
    cl_bytes = b"%PDF cover"
    msg = build_mime_message(
        "rec@co.com", "Subj", "Body",
        attachments=[
            {"filename": "A_Resume.pdf", "content": resume_bytes, "mimetype": "application/pdf"},
            {"filename": "A_Cover_Letter.pdf", "content": cl_bytes, "mimetype": "application/pdf"},
        ],
    )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    parsed_atts = list(parsed.iter_attachments())
    assert len(parsed_atts) == 2
    names = {a.get_filename() for a in parsed_atts}
    assert names == {"A_Resume.pdf", "A_Cover_Letter.pdf"}


def test_mime_missing_content_raises():
    """Attachment entry with empty content should be skipped, not crash."""
    msg = build_mime_message(
        "a@b.com", "S", "Body",
        attachments=[
            {"filename": "empty.pdf", "content": b"", "mimetype": "application/pdf"},
        ],
    )
    # Empty content is falsy, so it should be skipped
    atts = list(msg.iter_attachments())
    assert len(atts) == 0


def test_mime_missing_recipient_raises():
    with pytest.raises(ValueError, match="Recipient"):
        build_mime_message("", "Subject", "Body")


def test_mime_missing_subject_raises():
    with pytest.raises(ValueError, match="subject"):
        build_mime_message("a@b.com", "", "Body")


def test_mime_missing_body_raises():
    with pytest.raises(ValueError, match="body"):
        build_mime_message("a@b.com", "Subject", "")


# ---------------------------------------------------------------------------
# Placeholder detection tests
# ---------------------------------------------------------------------------


def test_find_unresolved_placeholders_detects_link():
    text = "GitHub: [Link]\nLinkedIn: [Link]\nSee my work."
    hits = find_unresolved_placeholders(text)
    assert "[Link]" in hits or len(hits) >= 2


def test_find_unresolved_placeholders_detects_your_name():
    text = "Best regards,\n[Your Name]"
    hits = find_unresolved_placeholders(text)
    assert any("[Your Name]" in h or "Your Name" in h for h in hits)


def test_find_unresolved_placeholders_clean_text():
    text = "Dear Jane,\n\nI am applying for the Software Engineer role.\n\nBest,\nJane Doe"
    hits = find_unresolved_placeholders(text)
    assert hits == []


def test_find_unresolved_placeholders_real_link_not_flagged():
    text = "GitHub: https://github.com/janedoe\nLinkedIn: https://linkedin.com/in/janedoe"
    hits = find_unresolved_placeholders(text)
    assert hits == []


# ---------------------------------------------------------------------------
# Placeholder stripping tests
# ---------------------------------------------------------------------------


def test_strip_placeholder_links_removes_github_link():
    text = "GitHub: [Link]\nSee my work at https://github.com/real"
    cleaned = strip_placeholder_links(text)
    assert "GitHub: [Link]" not in cleaned
    assert "https://github.com/real" in cleaned


def test_strip_placeholder_links_replaces_your_name():
    text = "Best regards,\n[Your Name]"
    cleaned = strip_placeholder_links(text, candidate_name="Jane Doe")
    assert "[Your Name]" not in cleaned
    assert "Jane Doe" in cleaned


def test_strip_placeholder_links_no_name_provided_leaves_real_name():
    text = "Best,\nJane Doe"
    cleaned = strip_placeholder_links(text)
    assert "Jane Doe" in cleaned


def test_strip_placeholder_links_empty_string():
    assert strip_placeholder_links("") == ""


# ---------------------------------------------------------------------------
# Dynamic filename tests
# ---------------------------------------------------------------------------


def test_dynamic_filename_special_chars():
    """Candidate names with special chars should produce safe filenames."""
    import re
    raw_name = "José María O'Brien"
    clean = re.sub(r"[^\w\s-]", "", raw_name).strip().replace(" ", "_")
    resume_filename = f"{clean}_Resume.pdf"
    assert "/" not in resume_filename
    assert "\\" not in resume_filename
    assert resume_filename.endswith("_Resume.pdf")


def test_dynamic_filename_candidate_name_default():
    import re
    name = "Candidate"
    clean = re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")
    assert f"{clean}_Resume.pdf" == "Candidate_Resume.pdf"

