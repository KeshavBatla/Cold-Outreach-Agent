"""
Evaluation package for Cold Outreach Agent.
"""

from evaluation.llm_evaluator import (
    ResearchExtraction,
    CompanyNameOnly,
    ResearchEvaluation,
    CoverLetterEvaluation,
    EmailEvaluation,
    EmailSubject,
    get_research_extractor,
    get_company_name_extractor,
    get_research_evaluator,
    get_coverletter_evaluator,
    get_email_evaluator,
    get_subject_generator,
)

__all__ = [
    "ResearchExtraction",
    "CompanyNameOnly",
    "ResearchEvaluation",
    "CoverLetterEvaluation",
    "EmailEvaluation",
    "EmailSubject",
    "get_research_extractor",
    "get_company_name_extractor",
    "get_research_evaluator",
    "get_coverletter_evaluator",
    "get_email_evaluator",
    "get_subject_generator",
]

