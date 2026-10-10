"""
CV Parser and Profile Generator utility module.
Extracts text from PDF and DOCX resumes, and summarizes CV text into a concise candidate profile.
"""

import os
from pathlib import Path
from typing import Union, BinaryIO, Optional
import pdfplumber
import docx
from langchain_core.messages import SystemMessage, HumanMessage

import config
from utils.helpers import extract_text


def extract_text_from_pdf(file_source: Union[str, Path, BinaryIO]) -> str:
    """
    Extract text content from a PDF file using pdfplumber.
    """
    text_parts = []
    with pdfplumber.open(file_source) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text.strip())
    return "\n\n".join(text_parts).strip()


def extract_text_from_docx(file_source: Union[str, Path, BinaryIO]) -> str:
    """
    Extract text content from a DOCX file using python-docx.
    """
    doc = docx.Document(file_source)
    text_parts = []
    for paragraph in doc.paragraphs:
        if paragraph.text and paragraph.text.strip():
            text_parts.append(paragraph.text.strip())
    for table in doc.tables:
        for row in table.rows:
            row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_texts:
                text_parts.append(" | ".join(row_texts))
    return "\n".join(text_parts).strip()


def extract_text_from_cv(file_source: Union[str, Path, BinaryIO], filename: Optional[str] = None) -> str:
    """
    Extract raw text from a CV file (PDF or DOCX).
    Determines type from filename or file_source extension.
    """
    name = filename or (str(file_source) if isinstance(file_source, (str, Path)) else "")
    lower_name = name.lower()

    if lower_name.endswith(".docx") or lower_name.endswith(".doc"):
        return extract_text_from_docx(file_source)
    elif lower_name.endswith(".pdf"):
        return extract_text_from_pdf(file_source)
    else:
        # Try PDF first, then fallback to DOCX or plain read
        try:
            return extract_text_from_pdf(file_source)
        except Exception:
            try:
                if hasattr(file_source, "seek"):
                    file_source.seek(0)
                return extract_text_from_docx(file_source)
            except Exception as e:
                raise ValueError(f"Unsupported or unparseable CV file format: {name}. Error: {e}")


def generate_candidate_profile(cv_text: str, llm=None) -> str:
    """
    Summarize extracted CV text into a short-form 'candidate_profile' string
    (2-4 sentences, key skills + technical focus area + target role types).
    """
    if not cv_text or not cv_text.strip():
        return ""

    model = llm or config.get_llm()

    system_prompt = (
        "You are an expert technical talent recruiter and career profiler. "
        "Analyze the provided candidate CV text and synthesize it into a punchy, high-impact "
        "short-form candidate profile (strictly 2 to 4 sentences). "
        "Include the candidate's core technical stack, primary domain focus "
        "(e.g., agentic AI, LLM systems, full-stack ML, backend engineering), "
        "and the types of opportunities/roles they are targeted toward. "
        "Return ONLY the concise candidate profile text. Do not include markdown headers, "
        "bullet points, quotes, or conversational preambles."
    )

    human_prompt = f"Candidate CV Text:\n\n{cv_text[:4000]}\n\nGenerate the 2-4 sentence candidate profile string."

    response = model.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ])

    return extract_text(response.content)

