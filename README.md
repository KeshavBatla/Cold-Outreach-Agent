# 🚀 Cold Outreach Multi-Agent System

An autonomous, multi-agent cold outreach system engineered with **LangGraph**, **Google Gemini**, **Tavily Web Search**, **Hunter.io**, and the **Gmail API**. The pipeline autonomously searches for relevant AI/ML engineering roles, discovers verified hiring manager and recruiter contacts, synthesizes candidate background to draft bespoke cover letters and emails, executes automated LLM-in-the-loop quality evaluation loops, and pauses for human verification before dispatching emails.

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
- **Research Caching**: SQLite-based caching prevents redundant Tavily/Hunter calls and enables rapid iteration.
- **Duplicate Protection**: Prevents contacting the same company/role within a configurable cooldown period.

---

## 🏛️ System Architecture

The master workflow uses a Supervisor StateGraph that coordinates four subgraphs in sequence:

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

**Workflow Sequence**:
1. **Research cache lookup** → Check if recent research exists for the query
2. **Research and extraction** → Tavily search → Structured data extraction
3. **Role-fit evaluation** → LLM evaluates if role matches candidate profile (agentic AI/LLM focus, entry-level)
4. **Duplicate/exclusion checks** → Checks against local exclusions and cooldowns
5. **Cached contacts or Hunter discovery** → Uses cached contacts or searches Hunter.io
6. **Bounded email verification** → Verifies up to 3 emails per company with verification freshness checks
7. **Explicit human research review** → User approves/rejects researched contact and role
8. **Cover-letter and email drafting** → Parallel generation with LLM evaluation/refinement loops
9. **Final human approval** → User reviews and approves final email before sending
10. **Delivery** → Sends via Gmail API (or simulates based on DRY_RUN/MOCK_MODE)

**Important**: Evaluating role fit *before* Hunter discovery is intentional to avoid spending contact-discovery credits on unsuitable roles.

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
- **Role Fit Evaluation**: Uses LLM to evaluate if discovered role focuses on agentic AI/LLM and matches entry-level/intern seniority.
- **Human-in-the-Loop Checkpoint**: Pauses for explicit user approval of research findings before proceeding.

### 3. Coverwriter Agent (`agents/cover_writer.py`)
- References real projects from `CANDIDATE_CV` and mirrors tone from `EXAMPLE_COVER_LETTER`.
- Enforces concise, 3-paragraph punchy structure (~250 words) focused strictly on verified experience.
- Iterates with the Cover Letter Evaluator until all criteria are met or max retries are reached.
- **Human-in-the-Loop Checkpoint**: Pauses for explicit user approval of cover letter before proceeding to email writing.

### 4. Emailwriter Agent (`agents/email_writer.py`)
- Drafts a short, 3-5 sentence scannable outreach message to accompany the attachments.
- References the attached resume and cover letter, personalized to the hiring manager.
- Iterates with the Email Evaluator to ensure brevity, tone, and call-to-action clarity.
- **Human-in-the-Loop Checkpoint**: Pauses for explicit user approval of email body before proceeding to final drafting.

### 5. Emaildrafter & Sender Agent (`agents/email_drafter.py`)
- Generates a clean, tailored email subject line.
- Compiles the cover letter into an A4 PDF and bundles it with the candidate's CV.
- Pauses for human confirmation before sending via Google Gmail API.
- Logs dispatch status to execution state.
- **Human-in-the-Loop Checkpoint**: Final explicit approval required before any email transmission.

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
- **Checkpoint 2 (Cover Letter Approval)**: Pauses after cover letter generation for user review and approval.
- **Checkpoint 3 (Email Body Approval)**: Pauses after email body generation for user review and approval.
- **Checkpoint 4 (Final Email Dispatch)**: Pauses execution after the email body, subject line, and PDF cover letter have been prepared. The user inspects the exact recipient, subject, and body before any message is transmitted.

---

## 🛠️ Tech Stack

- **Orchestration**: [LangGraph](https://github.com/langchain-ai/langgraph), LangChain Core
- **LLM**: Google Gemini (`models/gemini-2.5-flash` or `models/gemini-3.1-flash-lite`) via `langchain-google-genai`
- **Structured Extraction**: [Trustcall](https://github.com/hinthornw/trustcall), Pydantic v2
- **Search & Discovery**: Tavily Search API, Hunter.io API
- **Document Generation**: `fpdf2` (A4 typography with TrueType Unicode font support)
- **Email Delivery**: Google Workspace Gmail API (`google-api-python-client`, OAuth2)
- **User Interface**: Streamlit
- **Persistence**: SQLAlchemy 2.0 with SQLite (default) or PostgreSQL (configured via `DATABASE_URL`)
- **Mocking**: Deterministic fixtures for provider-free testing

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
├── utils/                          # Common helpers & validators
│   ├── __init__.py
│   └── helpers.py                  # Text extraction, deduplication, regex
│
├── persistence.py                  # SQLAlchemy-backed research, cache, exclusion, and outreach history
│
├── mocking.py                      # Deterministic integration fixtures used only when MOCK_MODE is enabled
│
└── tests/                          # Unit and integration tests
    ├── __init__.py
    ├── conftest.py                 # Pytest configuration
    └── ...                         # Test files
```

---

## ⚙️ Setup & Installation

### Supported Python versions

Use Python 3.12. The project is exercised with Python 3.12.

### 1. Clone & Enter Workspace
```powershell
git clone https://github.com/KeshavBatla/Cold-Outreach-Agent.git
cd Cold-Outreach-Agent
```

### 2. Create and Activate Virtual Environment
```powershell
py -3.12 -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# On Linux/macOS:
source .venv/bin/activate
```

### 3. Install Dependencies
```powershell
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
- Duplicate cooldown (`DUPLICATE_COOLDOWN_DAYS`), cache TTL (`RESEARCH_CACHE_TTL_DAYS`), verification freshness (`VERIFICATION_FRESHNESS_DAYS`), max contacts per company (`MAX_CONTACTS_PER_COMPANY`), max email verifications (`MAX_EMAIL_VERIFICATIONS`), and max external calls per run (`MAX_EXTERNAL_CALLS_PER_RUN`) can be configured using `.env.example`.

Do not put credentials, OAuth tokens, CVs, or generated PDFs under version control.

### MOCK_MODE and DRY_RUN Explained

| MOCK_MODE | DRY_RUN | Behavior |
|-----------|---------|----------|
| `true` | `true` | **Deterministic local fixtures**: Uses mock data from `mocking.py` for all provider calls (Tavily, Hunter, Gemini). No real provider calls are made. Email transmission is simulated. |
| `false` | `true` | **Real provider calls**: Makes actual calls to Tavily, Hunter, and Gemini APIs. Research and generation use live data. Email transmission is skipped (no real Gmail API calls). |
| `false` | `false` | **Full live execution**: Makes real provider calls and attempts real email delivery via Gmail API after explicit user approval. |

**Important**: `DRY_RUN` alone does not make the workflow free because Gemini, Tavily, and Hunter may still be called (and consume quotas/costs) when `MOCK_MODE=false`. Only when both `MOCK_MODE=true` and `DRY_RUN=true` is the workflow completely free and deterministic.

In mock mode, email delivery is simulated - no actual email is transmitted via Gmail API.

---

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

### Provider-Free Mock UI Launch Example

For deterministic, provider-free UI testing:
```powershell
$env:MOCK_MODE = "true"
$env:MOCK_SCENARIO = "good"
$env:DRY_RUN = "true"
streamlit run app.py
```

### Switching Between Testing Modes

To switch between testing modes, either:
1. Set environment variables in your current PowerShell session:
   ```powershell
   $env:MOCK_MODE = "false"
   $env:DRY_RUN = "false"
   ```
2. Or close and open a fresh terminal to ensure clean environment state.

Never expose real API keys in screenshots or shared configurations.

### Mock Scenarios

When `MOCK_MODE=true`, the `MOCK_SCENARIO` environment variable determines the behavior:

- `"good"`: Returns a valid company, role, and contact that passes all evaluations (default)
- `"role_mismatch"`: Returns a role that doesn't match the candidate profile (e.g., "Senior Sales Director") to test role-fit rejection
- `"no_contacts"`: Returns company/role but no usable contacts to test contact exhaustion handling
- `"invalid_contact"`: Returns an invalid email address to test email verification failure handling
- `"provider_timeout"`: Simulates a Tavily/Hunter API timeout to test error handling and retry logic

These scenarios are **deterministic fixtures** meant to test specific code paths. They do **not** verify:
- Real model availability or API quotas
- Live search quality or result relevance
- Actual Gmail OAuth flow or delivery
- Real-world contact accuracy from Hunter.io

### Schema Initialization & Persistence

- **SQLite is the local default**: Configured via `DATABASE_URL` environment variable (defaults to `sqlite:///runs/outreach.sqlite3`)
- **PostgreSQL is supported**: Through SQLAlchemy and psycopg 3 when `DATABASE_URL` is set to a PostgreSQL connection string
- **Persisted data includes**:
  - Research history (to enable caching and prevent redundant API calls)
  - Contact cache (to reduce Hunter.io API calls)
  - Exclusion list (companies rejected by user)
  - Provider usage tracking (for cost/quota monitoring)
  - Outreach delivery records (status, timestamps, message IDs)
- **Duplicate protection and cooldowns**:
  - Prevents re-contacting the same company within `DUPLICATE_COOLDOWN_DAYS` (default: 30 days)
  - Prevents re-contacting the same role at a company within the cooldown period
  - Respects user rejections via the exclusion list
- **Schema initialization**: Currently uses `Base.metadata.create_all()` on application startup
- **No Alembic migration history**: Schema migrations and production deployment safeguards remain future work

**Do not describe this database setup as production-ready**. It is suitable for local development and testing but lacks production safeguards like connection pooling, migration history, and robust error handling.

### Delivery Safety & Outcomes

The system implements multiple safety mechanisms to prevent unwanted email transmission:

- **Explicit Approval Required**: Four separate human-in-the-loop checkpoints require explicit user approval before proceeding
- **Dry-Run Behavior**: When `DRY_RUN=true`, the Gmail API `send()` method is called with `dry_run=true` parameter, which simulates sending without actual transmission
- **Duplicate Reservations**: Before sending, the system checks if a similar outreach (same company/recipient) was recently sent or is pending
- **Delivery Outcomes** (as recorded in `delivery_status` field):
  - `sent`: Email successfully transmitted via Gmail API
  - `dry_run`: Gmail API called in dry-run mode (no actual transmission)
  - `failed`: Gmail API returned an error (check logs for details)
  - `unknown`: Gmail API call completed but delivery status couldn't be determined
  - `blocked`: Outreach blocked due to duplicate protection or cooldowns
  - `simulated`: Used in `MOCK_MODE=true` - no Gmail API call made

**Important**: An ambiguous Gmail result (status: `unknown`) is **not** guaranteed to be delivered or guaranteed not to have been delivered. Users should check their Sent folder in Gmail before retrying to avoid duplicate emails.

---

## 🚧 Current Limitations & Next Steps

**Current State**: The application is a **local Streamlit prototype** with local Gmail OAuth authentication. It is designed for single-user operation on a development machine.

**Does NOT currently support**:
- A public multi-user website
- Per-user web OAuth and secure account isolation
- A bulk outreach review table or "Send All" feature
- Production migrations, deployment monitoring, or production-grade account security
- Horizontal scaling or concurrent user support
- Advanced analytics dashboard or response tracking

**These are presented as future work** if not implemented in the current version.

**Near-term improvements planned**:
- Google Sheets integration for outreach outcome tracking
- Inbound response tracking via Gmail API webhooks
- Vector store CV retrieval (RAG) for dynamic project snippet extraction
- Multi-channel outreach (LinkedIn connection notes)
- Production-ready deployment with user authentication and account isolation

---

## 🔒 Security & Credentials Hygiene

- **Strict `.gitignore`**: `.env`, OAuth tokens (`token.json`), client secrets (`credentials.json`), generated PDFs, and original notebooks are strictly excluded from version control.
- **Send-Only Gmail Scope**: The Gmail integration requests only `https://www.googleapis.com/auth/gmail.send` rather than full mailbox access.
- **Zero Hardcoded Secrets**: All API tokens and credentials are read exclusively from environment variables or local user secrets files.
- **Environment Isolation**: Each run uses an isolated directory under `runs/` to prevent cross-contamination of research caches and generated artifacts.

---

## 👨‍💻 Author & Links

- **Repository**: Cold Outreach Multi-Agent System
- **License**: MIT
- **Author**: Keshav Batla
- **Email**: batlakeshav@gmail.com
- **LinkedIn**: [linkedin.com/in/keshav-batla](https://www.linkedin.com/in/keshav-batla-715a38330/)
- **GitHub**: [github.com/keshavbatla](https://github.com/keshavbatla)
