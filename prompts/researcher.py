"""
Prompts used by the Researcher agent and Research Evaluator.
"""

RESEARCHER_QUERY_SYSTEM_PROMPT = """You are an expert search query generator for a cold outreach agent.
Candidate profile: {profile}
Mode: {mode}
Seed: {seed_query}

If the mode is "open_role": Frame a query to extract all necessary details of the seed company hiring for tech jobs relevant to the user's profile. Target company name, hiring manager/contact email, contact name, job role, and job description.
If the mode is "speculative": Frame a query to find general tech companies that usually hire interns based on the candidate profile. Target company name, HR/Talent Acquisition emails, and contact names/titles.
Seniority: target fresher-level or internship opportunities ONLY. Exclude anything asking for 3+ years of experience, or senior/staff/lead titles.
Domain focus: agentic AI, LLM applications, and multi-agent systems specifically — not generic backend engineering, and not roles that are primarily "RAG engineer" without a clear agentic/LLM-systems scope.
You must identify exactly ONE specific, real, named company — never a market survey or a list of multiple companies/postings.
Return ONLY the raw search query string. Do not include quotes, explanations, or conversational text."""

RESEARCHER_QUERY_HUMAN_PROMPT = "Generate the optimized search query string."

EXTRACTION_SYSTEM_PROMPT = (
    "Extract these fields from the research text. If multiple companies or postings are mentioned, "
    "pick only the SINGLE most specific, concrete, real company — never summarize across multiple "
    "companies. Leave a field empty if unclear — don't guess. IMPORTANT: company_name and "
    "research_summary must refer to the exact same company — if a company is named anywhere in "
    "research_summary, company_name must contain that same name."
)

COMPANY_NAME_FALLBACK_PROMPT = (
    "What single company is named in this text? Return its name, or leave empty if none is named."
)

RESEARCH_EVALUATION_SYSTEM_PROMPT = (
    "Judge whether this role is fresher/intern-level and genuinely agentic AI/LLM-focused."
)

