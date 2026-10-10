"""
CV Parser and Profile Generator utility module.
Extracts text from PDF and DOCX resumes, summarizes CV text into a candidate profile,
and builds run inputs for the outreach pipeline.
"""

import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union, BinaryIO, Optional, Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage

import config
from utils.helpers import extract_text


class RunInputs(dict):
    """
    Container for run inputs. Supports both dict-style access, attribute access,
    and 5-tuple unpacking: (raw_cv_text, candidate_profile, candidate_name, resume_path, example_cover_letter).
    """
    def __init__(
        self,
        raw_cv_text: str,
        candidate_profile: str,
        candidate_name: str,
        resume_path: str,
        example_cover_letter: Optional[str] = None,
    ):
        data = {
            "raw_cv_text": raw_cv_text,
            "candidate_profile": candidate_profile,
            "candidate_name": candidate_name,
            "resume_path": resume_path,
            "example_cover_letter": example_cover_letter,
        }
        super().__init__(data)
        self.raw_cv_text = raw_cv_text
        self.candidate_profile = candidate_profile
        self.candidate_name = candidate_name
        self.resume_path = resume_path
        self.example_cover_letter = example_cover_letter

    def __iter__(self):
        return iter([
            self.raw_cv_text,
            self.candidate_profile,
            self.candidate_name,
            self.resume_path,
            self.example_cover_letter,
        ])


def extract_text_from_pdf(file_source: Union[str, Path, BinaryIO]) -> str:
    """
    Extract text content from a PDF file using pypdf, with pdfplumber fallback.
    """
    # Try pypdf first
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_source)
        text_parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t.strip())
        extracted = "\n\n".join(text_parts).strip()
        if extracted:
            return extracted
    except Exception:
        pass

    # Try pdfplumber fallback
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_source) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text.strip())
        return "\n\n".join(text_parts).strip()
    except Exception:
        pass

    return ""


def extract_text_from_docx(file_source: Union[str, Path, BinaryIO]) -> str:
    """
    Extract text content from a DOCX file using python-docx or standard library zipfile.
    """
    try:
        import docx
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
    except Exception:
        pass

    # Fallback using standard library zipfile & XML parser
    try:
        with zipfile.ZipFile(file_source) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            paragraphs = []
            for node in tree.iter():
                if node.tag.endswith("}p"):
                    p_texts = [sub.text for sub in node.iter() if sub.tag.endswith("}t") and sub.text]
                    if p_texts:
                        paragraphs.append("".join(p_texts).strip())
            return "\n".join(paragraphs).strip()
    except Exception as e:
        raise ValueError(f"Unable to extract text from DOCX file: {e}")


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
        # Try PDF first, then fallback to DOCX
        try:
            extracted = extract_text_from_pdf(file_source)
            if extracted:
                return extracted
        except Exception:
            pass

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

    try:
        model = llm or config.get_llm()
    except Exception:
        # Offline or missing API key fallback
        lines = [l.strip() for l in cv_text.splitlines() if l.strip()]
        return " ".join(lines[:4])[:300]

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

    try:
        response = model.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ])
        return extract_text(response.content)
    except Exception:
        lines = [l.strip() for l in cv_text.splitlines() if l.strip()]
        return " ".join(lines[:4])[:300]


def extract_candidate_name(cv_text: str, llm=None) -> str:
    """
    Extract the candidate's full name from CV text.
    Uses LLM if available, with a reliable heuristic fallback.
    """
    if not cv_text or not cv_text.strip():
        return "Candidate"

    lines = [line.strip() for line in cv_text.splitlines() if line.strip()]
    default_name = lines[0] if lines else "Candidate"

    # Attempt LLM extraction if key configured
    if config.GEMINI_API_KEY or llm is not None:
        try:
            model = llm or config.get_llm()
            system_prompt = (
                "You are an expert resume parser. Extract ONLY the full name of the candidate from the provided CV text. "
                "Return ONLY the candidate's full name without punctuation, labels, or additional commentary."
            )
            human_prompt = f"CV Text:\n\n{cv_text[:1200]}\n\nCandidate full name:"
            response = model.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ])
            extracted = extract_text(response.content).strip()
            # Clean possible markdown or quotes
            extracted = extracted.strip("\"'*`").splitlines()[0].strip()
            if extracted and len(extracted) < 60:
                return extracted
        except Exception:
            pass

    # Heuristic fallback: inspect first few lines
    for line in lines[:5]:
        cleaned = re.sub(r"[^a-zA-Z\s\.\-']", "", line).strip()
        words = cleaned.split()
        if 2 <= len(words) <= 4 and len(cleaned) < 50:
            lower = cleaned.lower()
            if not any(kw in lower for kw in ["curriculum", "resume", "cv", "page", "email", "phone"]):
                return cleaned

    return default_name


def build_run_inputs(
    cv_path: Union[str, Path],
    example_letter_path: Optional[Union[str, Path]] = None,
    run_dir: Optional[Union[str, Path]] = None,
    llm=None,
) -> RunInputs:
    """
    Validate CV file format, extract text, verify non-empty text (not a scanned PDF),
    generate candidate profile, extract candidate name, optionally isolate files in run_dir,
    and return structured run inputs.
    """
    p_cv = Path(cv_path)
    if not p_cv.exists():
        raise FileNotFoundError(f"CV file not found: {p_cv}")

    suffix = p_cv.suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise ValueError(
            f"Unsupported CV file type '{suffix}'. Only .pdf and .docx files are supported."
        )

    # If run_dir is provided, copy CV to run_dir to isolate it
    if run_dir:
        run_dir_path = Path(run_dir)
        run_dir_path.mkdir(parents=True, exist_ok=True)
        dest_cv = run_dir_path / p_cv.name
        if p_cv.resolve() != dest_cv.resolve():
            shutil.copy2(p_cv, dest_cv)
        resolved_resume_path = str(dest_cv.resolve())
        working_cv_source = dest_cv
    else:
        resolved_resume_path = str(p_cv.resolve())
        working_cv_source = p_cv

    # Extract text from CV
    raw_cv_text = extract_text_from_cv(working_cv_source, filename=p_cv.name)

    # Check for empty text or scanned PDF (< 200 characters)
    if not raw_cv_text or len(raw_cv_text.strip()) < 200:
        raise ValueError(
            "Extracted CV text is empty or under 200 characters. "
            "If this is a scanned PDF, please provide a text-searchable PDF or DOCX."
        )

    # Extract optional example letter text
    example_cover_letter = None
    if example_letter_path:
        p_ex = Path(example_letter_path)
        if p_ex.exists():
            if run_dir:
                dest_ex = Path(run_dir) / p_ex.name
                if p_ex.resolve() != dest_ex.resolve():
                    shutil.copy2(p_ex, dest_ex)
                working_ex_source = dest_ex
            else:
                working_ex_source = p_ex

            ex_suffix = working_ex_source.suffix.lower()
            if ex_suffix in (".pdf", ".docx"):
                example_cover_letter = extract_text_from_cv(working_ex_source, filename=p_ex.name)
            else:
                try:
                    with open(working_ex_source, "r", encoding="utf-8", errors="ignore") as f:
                        example_cover_letter = f.read().strip()
                except Exception:
                    example_cover_letter = None

    # Generate candidate profile & candidate name
    candidate_profile = generate_candidate_profile(raw_cv_text, llm=llm)
    candidate_name = extract_candidate_name(raw_cv_text, llm=llm)

    return RunInputs(
        raw_cv_text=raw_cv_text,
        candidate_profile=candidate_profile,
        candidate_name=candidate_name,
        resume_path=resolved_resume_path,
        example_cover_letter=example_cover_letter,
    )
