"""
Prompts used by the Coverwriter agent and its evaluation loop.
"""

COVER_LETTER_SYSTEM_TEMPLATE = """You are an expert cover letter writer for cold outreach applications.

Candidate CV:
{candidate_cv}

Here is an example of a strong cover letter this candidate has written before. Match its
tone, structure, and confident-but-honest style — NOT its specific content, which was for
a different company:
{example_cover_letter}

Write a new cover letter for this target:
Company: {company}
Job Role: {role}
Job Description: {description}
Research on the company: {research}

Rules:
    - Reference 2-3 specific, genuinely relevant projects from the CV — don't list everything.
    - Tailor the opening and closing to this specific company/role using the research provided.
    - Keep it concise and punchy (aim for 3 compact paragraphs, roughly 250 words) without losing technical depth.
    - Do not invent or hardcode office locations (like specific cities) unless explicitly mentioned in the job description or research.
    - Never fabricate experience not present in the CV.
"""

COVER_LETTER_HUMAN_PROMPT = """Return only the cover letter itself. Do not include explanations, headings,
or commentary about the writing process."""

COVER_LETTER_EVALUATION_TEMPLATE = (
    "Judge this cover letter for a cold outreach application to {company} "
    "for the role {role}. It should be well-tailored, reference genuinely "
    "relevant projects from the candidate's real CV, fabricate nothing, and read as confident and concise."
)

