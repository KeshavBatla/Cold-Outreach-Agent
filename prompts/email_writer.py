"""
Prompts used by the Emailwriter and Emaildrafter agents and their evaluation loops.
"""

EMAIL_WRITER_SYSTEM_TEMPLATE = """You are writing a short, professional cold outreach email to accompany a resume and cover letter.

Candidate: Keshav Batla, B.Tech CS student at Thapar Institute of Engineering and Technology,
focused on agentic AI, LLM systems, and RAG pipelines.

Target:
Company: {company}
Contact: {contact_name}
Role: {role}
Research on the company: {research}

A full cover letter has already been written separately (attached, not repeated here):
{cover_letter}

Write the EMAIL BODY only — this is NOT the cover letter, it's the short message that
introduces and accompanies it. Rules:
- 3-5 sentences max. Cold outreach emails should be short and scannable.
- Address {contact_name} by name if it's a real name; otherwise use a neutral greeting.
- Mention the specific role and one concrete, relevant detail from the candidate's background.
- Reference that the resume and cover letter are attached.
- End with a clear, low-friction call to action (e.g. open to a quick call).
- Do not repeat the full cover letter content.
- No subject line, no formal signature block beyond a simple sign-off.
"""

EMAIL_WRITER_HUMAN_PROMPT = (
    "Return only the email body text. No explanations, no headings, no subject line."
)

EMAIL_EVALUATION_TEMPLATE = (
    "Judge this cold outreach email for {company} ({role}). "
    "It must be short (3-5 sentences), personalized, mention the attached resume/cover letter, "
    "end with a clear call to action, and not just repeat the full cover letter content."
)

EMAIL_SUBJECT_SYSTEM_TEMPLATE = (
    "Write a short, professional cold outreach email subject line for "
    "Keshav Batla applying to a {role} role at {company}. "
    "Return a complete subject line with no placeholders or brackets."
)

