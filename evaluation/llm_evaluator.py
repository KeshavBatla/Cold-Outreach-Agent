"""
Structured Pydantic schemas and extractor builders for LLM extraction and evaluations.
"""

from typing import Optional
from pydantic import BaseModel, Field
from trustcall import create_extractor


class ResearchExtraction(BaseModel):
    company_name: Optional[str] = Field(
        None,
        description=(
            "Name of the company mentioned in the research text. Leave this EMPTY/None if no specific, "
            "real company is clearly identifiable — never write placeholder text like 'Not specified', "
            "'Unknown', or 'N/A'."
        ),
    )
    contact_name: Optional[str] = Field(
        None,
        description="Name of a person, recruiter, or manager if mentioned.",
    )
    contact_email: Optional[str] = Field(
        None,
        description="Email address if explicitly mentioned in text.",
    )
    job_role: Optional[str] = Field(
        None,
        description=(
            "The single specific job title/position targeted. "
            "CRITICAL: Pick EXACTLY ONE role. NEVER include slashes ('/'), ampersands ('&'), or multi-role lists "
            "(e.g., convert 'Agentic AI / GenAI Engineer' to 'Agentic AI Engineer'). "
            "Analyze the provided text and extract a SINGLE primary job role.\n\n"
            "Rules for Role Disambiguation:\n"
            "1. NO MULTIPLE ROLES: Never return combinations like 'Role A / Role B' or 'Role A & Role B'.\n"
            "2. TARGET PRIORITY: If multiple AI/Tech sub-fields are mentioned (e.g., GenAI, Agentic AI, ML, Data Science):\n"
            "   - Priority 1: 'Agentic AI / Autonomous Agents / LLM Orchestration' (highest priority)\n"
            "   - Priority 2: 'Generative AI / LLM Engineering'\n"
            "   - Priority 3: 'Machine Learning / Data Science'\n"
            "3. LEVEL DISAMBIGUATION: If both 'Engineer' and 'Intern' are listed, check the experience criteria:\n"
            "   - If target is entry-level/fresher, choose the 'Intern' or 'Junior/Early-Career' title variant."
        ),
    )
    job_description: Optional[str] = Field(
        None, description="Summary of the job description/requirements"
    )
    research_summary: Optional[str] = Field(
        None,
        description="Concise summary of findings useful for a cold outreach cover letter and email",
    )


class CompanyNameOnly(BaseModel):
    company_name: Optional[str] = Field(
        None, description="The single company name mentioned in this text, if any"
    )


class ResearchEvaluation(BaseModel):
    company_relevant: bool = Field(
        True,
        description="True if the company operates in the target industry, domain, or AI/tech space.",
    )
    role_suitable: bool = Field(
        True,
        description="True if the specific role matches the technical domain and job requirements.",
    )
    seniority_match: bool = Field(
        True,
        description="True if the required seniority matches requested seniority, or if experience requirements are unspecified.",
    )
    experience_required: Optional[str] = Field(
        None,
        description="Experience or seniority level explicitly mentioned in the posting, or 'unspecified'.",
    )
    is_sufficient: bool = Field(
        ...,
        description=(
            "True if the company is relevant, role is suitable, and seniority matches the requested profile. "
            "False if there is a clear mismatch in domain, role, or seniority."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description="Concise human-readable reason summarizing the decision.",
    )
    feedback: str = Field(
        ...,
        description="If not sufficient, what's missing so the next attempt can target it",
    )


class CoverLetterEvaluation(BaseModel):
    is_sufficient: bool = Field(
        ...,
        description=(
            "True if the letter is well-tailored to this specific company/role, references "
            "genuinely relevant CV projects, fabricates nothing, and matches the example's tone/quality"
        ),
    )
    feedback: str = Field(
        ..., description="If not sufficient, specific instructions to fix it"
    )


class EmailEvaluation(BaseModel):
    is_sufficient: bool = Field(
        ...,
        description=(
            "True if the email is short (3-5 sentences), personalized to the specific company/role/contact, "
            "mentions the attached resume/cover letter, ends with a clear call to action, and does not "
            "just repeat the full cover letter"
        ),
    )
    feedback: str = Field(
        ..., description="If not sufficient, specific instructions to fix it"
    )


class EmailSubject(BaseModel):
    subject: str = Field(
        ..., description="Short, professional cold outreach email subject line"
    )


def get_research_extractor(llm):
    return create_extractor(llm, tools=[ResearchExtraction], tool_choice="ResearchExtraction")


def get_company_name_extractor(llm):
    return create_extractor(llm, tools=[CompanyNameOnly], tool_choice="CompanyNameOnly")


def get_research_evaluator(llm):
    return create_extractor(llm, tools=[ResearchEvaluation], tool_choice="ResearchEvaluation")


def get_coverletter_evaluator(llm):
    return create_extractor(llm, tools=[CoverLetterEvaluation], tool_choice="CoverLetterEvaluation")


def get_email_evaluator(llm):
    return create_extractor(llm, tools=[EmailEvaluation], tool_choice="EmailEvaluation")


def get_subject_generator(llm):
    return create_extractor(llm, tools=[EmailSubject], tool_choice="EmailSubject")

