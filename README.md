# 🚀 Cold Outreach Multi-Agent System

An autonomous, multi-agent cold outreach system engineered with **LangGraph**, **Google Gemini**, **Tavily Web Search**, **Hunter.io**, and the **Gmail API**. The pipeline autonomously searches for relevant AI/ML engineering roles, discovers verified hiring manager and recruiter contacts, synthesizes candidate background to draft bespoke cover letters and emails, executes automated LLM-in-the-loop quality evaluation loops, and pauses for human verification before dispatching emails.

---

## 📌 Table of Contents
- [Overview](#overview)
- [Key Features](#key-features)
- [System Architecture](#system-architecture)
- [Agent Descriptions](#agent-descriptions)
- [Evaluation & Refinement Architecture](#evaluation--refinement-architecture)
- [Human-in-the-Loop (HITL) Architecture](#human-in-the-loop-hitl-architecture)
- [Tech Stack](#tech-stack)
- [Repository Structure](#repository-structure)
- [Setup & Installation](#setup--installation)
- [Environment Variables](#environment-variables)
- [Running Locally](#running-locally)
  - [CLI Mode](#cli-mode)
  - [Streamlit Web Interface](#streamlit-web-interface)
- [Security & Credentials Hygiene](#security--credentials-hygiene)
- [Planned Integrations & Future Roadmap](#planned-integrations--future-roadmap)
- [Author & Links](#author--links)

---

## 💡 Overview

Landing internships or early-career roles at competitive AI/ML tech companies requires targeted, highly personalized cold outreach. Generic outreach emails and mass-blast templates suffer from low open and response rates. 

This system solves that by deploying specialized, autonomous agents that:
1. Identify high-fit companies and positions tailored specifically to agentic AI, LLM orchestration, and RAG engineering.
2. Locate and verify direct hiring manager or technical recruiter email addresses.
3. Generate personalized cover letters grounded strictly in real candidate projects without hallucinations.
4. Run self-correcting evaluation loops that critique drafts and refine them before human eyes ever see them.
5. Provide safe, interruptible checkpoints where a human can approve or reject both the research target and the final dispatch.

---

## ✨ Key Features

- **Hierarchical LangGraph Architecture**: A centralized Supervisor/Manager orchestrates specialized worker subgraphs with independent state checkpointers (`MemorySaver`).
- **Targeted Role & Candidate Fit**: Evaluates roles with explicit bias toward agentic AI / LLM applications and fresher/intern seniority, automatically rejecting misaligned postings.
- **Automated Contact Scoring & Verification**: Domain-level contact discovery with rank-scoring prioritizing Engineering Managers, Directors of AI/ML, and Technical Recruiters, cross-verified with Hunter.io's email verifier.
- **Closed-Loop LLM Evaluation & Refinement**: Cover letter and email writer agents run internal critique loops against structured Pydantic schemas, correcting deficiencies across multiple attempts.
- **Dynamic PDF Generation**: Renders professional A4 cover letters using `fpdf2` and TrueType typography (DejaVu Sans), auto-attaching them alongside the candidate resume.
- **Dual Interface (CLI & Web UI)**: Execute either headlessly via programmatic CLI (`main.py`) or visually via a Streamlit dashboard (`app.py`).
- **Dry-Run Protection**: Built-in simulation mode preventing accidental email delivery during development or testing.

---

## 🏛️ System Architecture

The master workflow uses a Supervisor StateGraph that coordinates four subgraphs:

```
                            ┌────────────────────────┐
                            │    SUPERVISOR /        │
                            │      MANAGER           │
                            └───────────┬────────────┘
                                        │
     ┌──────────────────┬───────────────┴───────────────┬──────────────────┐
     ▼                  ▼                               ▼                  ▼
┌──────────────┐  ┌──────────────┐              ┌──────────────┐  ┌────────────────┐
│  RESEARCHER  │  │ COVER WRITER │              │ EMAIL WRITER │  │ EMAIL DRAFTER  │
│   SUBGRAPH   │  │   SUBGRAPH   │              │   SUBGRAPH   │  │  & DISPATCHER  │
└──────┬───────┘  └──────┬───────┘              └──────┬───────┘  └───────┬────────┘
       │                 │                             │                  │
 ┌─────▼─────┐     ┌─────▼──────┐                ┌─────▼──────┐     ┌─────▼──────┐
 │ Tavily &  │     │ Cover Gen  │                │ Email Gen  │     │ Subject    │
 │  Hunter   │     │    Node    │                │    Node    │     │ Generator  │
 └─────┬─────┘     └─────┬──────┘                └─────┬──────┘     └─────┬──────┘
       │                 │                             │                  │
 ┌─────▼─────┐     ┌─────▼──────┐                ┌─────▼──────┐     ┌─────▼──────┐
 │ Role Fit  │     │    LLM     │◄─(Refine Loop)─│    LLM     │     │   HUMAN    │
 │ Evaluator │     │ Evaluator  │                │ Evaluator  │     │  APPROVAL  │
 └─────┬─────┘     └────────────┘                └────────────┘     └─────┬──────┘
       │                                                                  │
 ┌─────▼─────┐                                                      ┌─────▼──────┐
 │   HUMAN   │                                                      │ Gmail API  │
 │ APPROVAL  │                                                      │  Dispatch  │
 └───────────┘                                                      └────────────┘
```

---

## 🤖 Agent Descriptions

### 1. Manager / Supervisor (`agents/manager.py`)
- Evaluates the top-level `OutreachState` and routes execution sequentially: `Researcher -> Coverwriter -> Emailwriter -> Emaildrafter`.
- Enforces maximum attempt limits per stage to avoid infinite retry loops.

### 2. Researcher Agent (`agents/researcher.py`)
- **Query Generation**: Converts candidate background and mode (`open_role` vs `speculative`) into an optimized Tavily search query string.
- **Structured Extraction**: Extracts single company name, job role, job description, and research summary using `trustcall`.
- **Contact Discovery & Scoring**: Queries Hunter.io domain search, scores contacts by title/department priority, and validates emails.
- **Verification Loop**: If a candidate email fails verification, iteratively steps to the next candidate contact.

### 3. Coverwriter Agent (`agents/cover_writer.py`)
- References real projects from `CANDIDATE_CV` and mirrors tone from `EXAMPLE_COVER_LETTER`.
- Enforces concise, 3-paragraph punchy structure (~250 words) focused strictly on verified experience.
- Iterates with the Cover Letter Evaluator until all criteria are met or max retries are reached.

### 4. Emailwriter Agent (`agents/email_writer.py`)
- Drafts a short, 3-5 sentence scannable outreach message to accompany the attachments.
- References the attached resume and cover letter, personalized to the hiring manager.
- Iterates with the Email Evaluator to ensure brevity, tone, and call-to-action clarity.

### 5. Emaildrafter & Sender Agent (`agents/email_drafter.py`)
- Generates a clean, tailored email subject line.
- Compiles the cover letter into an A4 PDF and bundles it with the candidate's CV.
- Pauses for human confirmation before sending via Google Gmail API.
- Logs dispatch status to execution state (`excelentry` placeholder).

---

## 🔄 Evaluation & Refinement Architecture

Unlike simple one-pass LLM prompts, the agents implement closed-loop reflection:
1. **Researcher Evaluation**: Validates role fit (`ResearchEvaluation`) — checks if the discovered role genuinely focuses on agentic AI / LLMs and matches entry-level / intern seniority.
2. **Cover Letter Evaluation**: Evaluates whether the generated letter references real CV projects, avoids hallucinated locations/facts, and matches professional standards (`CoverLetterEvaluation`).
3. **Email Evaluation**: Checks that the email is strictly 3-5 sentences, includes a clear call-to-action, and does not redundantly repeat the full cover letter (`EmailEvaluation`).

Each evaluator returns a structured boolean (`is_sufficient`) and qualitative `feedback`. If a draft is insufficient and retries remain, the generation node re-executes with the previous feedback injected into its system prompt.

---

## 👤 Human-in-the-Loop (HITL) Architecture

Human oversight is built directly into the graph via LangGraph interrupt checkpoints:
- **Checkpoint 1 (Research Review)**: Pauses execution after company, role, and contact email have been discovered and verified. The user can review the findings and either approve or reject (which triggers re-search).
- **Checkpoint 2 (Final Email Dispatch)**: Pauses execution after the email body, subject line, and PDF cover letter have been prepared. The user inspects the exact recipient, subject, and body before any message is transmitted.

---

## 🛠️ Tech Stack

- **Orchestration**: [LangGraph](https://github.com/langchain-ai/langgraph), LangChain Core
- **LLM**: Google Gemini (`models/gemini-2.5-flash` or `models/gemini-3.1-flash-lite`) via `langchain-google-genai`
- **Structured Extraction**: [Trustcall](https://github.com/hinthornw/trustcall), Pydantic v2
- **Search & Discovery**: Tavily Search API, Hunter.io API
- **Document Generation**: `fpdf2` (A4 typography with TrueType Unicode font support)
- **Email Delivery**: Google Workspace Gmail API (`google-api-python-client`, OAuth2)
- **User Interface**: Streamlit

---

## 📁 Repository Structure

```
cold-outreach-agent/
│
├── .gitignore                      # Excludes .env, credentials, PDFs, checkpoints
├── .env.example                    # Template environment variables
├── requirements.txt                # Exact project dependencies
├── README.md                       # Comprehensive documentation
│
├── config.py                       # Settings, paths, LLM instantiation
├── main.py                         # Programmatic CLI entry point
├── app.py                          # Interactive Streamlit web application
│
├── agents/                         # Agent definitions & subgraph builders
│   ├── __init__.py
│   ├── manager.py                  # Supervisor orchestrator
│   ├── researcher.py               # Research & contact discovery agent
│   ├── cover_writer.py             # Cover letter generation & critique loop
│   ├── email_writer.py             # Cold email body generation & critique loop
│   └── email_drafter.py            # Subject generation, PDF packaging & dispatch
│
├── evaluation/                     # Pydantic schemas & extractor factories
│   ├── __init__.py
│   └── llm_evaluator.py            # Extraction & evaluation models
│
├── graph/                          # LangGraph state & workflow compilation
│   ├── __init__.py
│   ├── state.py                    # OutreachState & subgraph state schemas
│   └── workflow.py                 # Graph builders & execution runner
│
├── prompts/                        # System prompts & candidate profile data
│   ├── __init__.py
│   ├── candidate.py                # Candidate CV, profile, & example cover letter
│   ├── researcher.py               # Researcher query & extraction prompts
│   ├── cover_writer.py             # Cover letter writing & evaluation prompts
│   └── email_writer.py             # Email drafting & evaluation prompts
│
├── tools/                          # External tool integrations
│   ├── __init__.py
│   ├── tavily_search.py            # Tavily Search API wrapper
│   ├── hunter.py                   # Hunter.io domain search & verifier
│   ├── gmail.py                    # Gmail OAuth service & email sender
│   └── pdf_generator.py            # FPDF PDF generator with DejaVu fonts
│
└── utils/                          # Common helpers & validators
    ├── __init__.py
    └── helpers.py                  # Text extraction, deduplication, regex
```

---

## ⚙️ Setup & Installation

### Supported Python versions

Use Python 3.11 or 3.12. The project is exercised with Python 3.12; no claim is made that a specific Gemini model or provider quota is available for every account.

### 1. Clone & Enter Workspace
```bash
git clone https://github.com/KeshavBatla/Cold-Outreach-Agent.git
cd Cold-Outreach-Agent
```

### 2. Create and Activate Virtual Environment
```bash
py -3.12 -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# On Linux/macOS:
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## 🔑 Environment Variables

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

On Linux/macOS use `cp .env.example .env`. Configure credentials for any live provider you plan to use. `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `TAVILY_API_KEY`, and `HUNTER_API_KEY` are needed for their respective live integrations. Gmail additionally requires a local OAuth `credentials.json` and authorizes locally when first used.

Important settings:

- `MODEL_NAME` selects the Gemini model. The default is `models/gemini-3.1-flash-lite`; verify access and quotas with your own Google account.
- `MOCK_MODE=true` switches provider and generation paths to deterministic fixtures and does not require API keys.
- `DRY_RUN=true` skips Gmail delivery, while allowing live research/generation if `MOCK_MODE=false`.
- `DATABASE_URL` defaults to an ignored local SQLite database under `runs/`. PostgreSQL can be configured with `postgresql://user:password@host:5432/database` using psycopg 3.
- `OUTREACH_OWNER_ID` identifies the current local owner for exclusions; it is not an authentication mechanism.
- Duplicate cooldown, cache, verification freshness, contact and external-call limits can be configured using `.env.example`.

Do not put credentials, OAuth tokens, CVs, or generated PDFs under version control.

## 💻 Running Locally

The CLI requires a local CV:

```powershell
python main.py --cv path\to\resume.pdf --company "Cohere" --dry-run
python main.py --cv path\to\resume.pdf --mode speculative --dry-run
```

Launch the Streamlit UI (a CV upload is required before starting a run):

```powershell
streamlit run app.py
```

For deterministic, provider-free UI testing:

```powershell
$env:MOCK_MODE = "true"
$env:MOCK_SCENARIO = "good"
$env:DRY_RUN = "false"
streamlit run app.py
```

`MOCK_MODE` is independent of `DRY_RUN`: mock mode never transmits email, even when dry-run is false. `MOCK_SCENARIO` accepts `good`, `role_mismatch`, `no_contacts`, `invalid_contact`, `provider_timeout`, and `failed_send`.

Initialize the schema by starting the application or running:

```powershell
python -c "import persistence; persistence.get_engine()"
```

Schema initialization uses SQLAlchemy `create_all` and is repeatable; no Alembic migration history exists yet. For schema changes, back up the database and apply a planned migration before deploying the changed model.

Run the automated tests (external services are mocked and tests never send real email):

```powershell
python -m pytest -q
```

Inspect the graph with `python main.py --inspect-graph`.

---

## 🔒 Security & Credentials Hygiene

- **Strict `.gitignore`**: `.env`, OAuth tokens (`token.json`), client secrets (`credentials.json`), generated PDFs, and original notebooks are strictly excluded from version control.
- **Send-Only Gmail Scope**: The Gmail integration requests only `https://www.googleapis.com/auth/gmail.send` rather than full mailbox access.
- **Zero Hardcoded Secrets**: All API tokens and credentials are read exclusively from environment variables or local user secrets files.

---

## 🗺️ Planned Integrations & Future Roadmap

- [ ] **Google Sheets Integration**: Automatically sync outreach outcomes, email message IDs, and contact info into a central Google Sheet.
- [ ] **Inbound Response Tracking**: Periodically check for replies via Gmail API webhooks and update application status.
- [ ] **Vector Store CV Retrieval (RAG)**: Dynamically retrieve specific bullet points or project snippets from a multi-page resume based on semantic similarity to the job description.
- [ ] **Multi-Channel Outreach**: Extend beyond email to draft LinkedIn connection notes and outreach messages.

---

## 👨‍💻 Project Links

- **Repository**: Cold Outreach Multi-Agent System
- **License**: MIT
- **Author**: Keshav Batla
- **Email**: batlakeshav@gmail.com
- **LinkedIn**: [linkedin.com/in/keshav-batla](https://www.linkedin.com/in/keshav-batla-715a38330/)
- **GitHub**: [github.com/keshavbatla](https://github.com/keshavbatla)
