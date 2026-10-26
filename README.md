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

On Linux/macOS use `cp .env.example .env`. Configure credentials for any live provider you plan to use. `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `TAVILY_API_KEY`, and `HUNTER_API_KEY` are needed for their respective live integrations. The legacy Streamlit app uses its local OAuth client file; the web dashboard uses the backend-only Google OAuth web-client variables below.

Important settings:

- `MODEL_NAME` selects the Gemini model. The default is `models/gemini-3.1-flash-lite`; verify access and quotas with your own Google account.
- `MOCK_MODE=true` switches provider and generation paths to deterministic fixtures and does not require API keys.
- The web dashboard uses `GMAIL_TEST_MODE=true` by default; its explicit send action records `simulated` without contacting Gmail. `GMAIL_LIVE_SEND_ENABLED` defaults to false and is independent of the legacy Streamlit `DRY_RUN` setting.
- `DATABASE_URL` defaults to an ignored local SQLite database under `runs/`. PostgreSQL can be configured with `postgresql://user:password@host:5432/database` using psycopg 3.
- `OUTREACH_OWNER_ID` identifies the current local owner for exclusions; it is not an authentication mechanism.
- Duplicate cooldown (`DUPLICATE_COOLDOWN_DAYS`), cache TTL (`RESEARCH_CACHE_TTL_DAYS`), verification freshness (`VERIFICATION_FRESHNESS_DAYS`), max contacts per company (`MAX_CONTACTS_PER_COMPANY`), max email verifications (`MAX_EMAIL_VERIFICATIONS`), max external calls per run (`MAX_EXTERNAL_CALLS_PER_RUN`), and speculative search budget (`SPECULATIVE_SEARCH_BUDGET`, 1–10 opportunities; default 4) can be configured using `.env.example`.

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

### Schema Migrations & Persistence

- **SQLite is the local default**: Configured via `DATABASE_URL` environment variable (defaults to `sqlite:///runs/outreach.sqlite3`)
- **PostgreSQL is supported**: Through SQLAlchemy and psycopg 3 when `DATABASE_URL` is set to a PostgreSQL connection string
- **Local PostgreSQL URL**: Store `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/cold_outreach` in your ignored `.env`, replacing the placeholders locally. Do not commit credentials. SQLite remains the runtime default unless you explicitly configure a different URL.
- **Apply migrations**: After installing requirements, create an empty database and run the Alembic baseline. These commands are instructions only; they were not run against your PostgreSQL installation:

  ```powershell
  Copy-Item .env.example .env
  # Edit .env locally; do not paste or commit a real password.
  psql -U postgres -c "CREATE DATABASE cold_outreach;"
  python -m alembic upgrade head
  ```

  Alembic creates schema objects only and does not import SQLite data.
- **Back up before a future SQLite import**: Stop the application first, then make and verify a backup with the SQLite CLI:

  ```powershell
  sqlite3 runs/outreach.sqlite3 ".backup 'runs/outreach-backup.sqlite3'"
  sqlite3 runs/outreach-backup.sqlite3 "PRAGMA integrity_check;"
  sqlite3 runs/outreach-backup.sqlite3 ".tables"
  ```

  `integrity_check` should return `ok`. Verify expected table row counts and retain both the original and verified backup until a separately reviewed import has been validated. Do not treat `alembic upgrade` as a data migration.
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
- **Runtime schema initialization**: The legacy SQLite workflow retains its local schema initialization. PostgreSQL web application schema is migration-managed; the API does not create missing tables.
- **Migration scope**: The original baseline captures the existing persistence models. A separate additive migration contains the web application tables.

**Do not describe this database setup as production-ready**. It is suitable for local development and testing but lacks production safeguards like connection pooling, migration history, and robust error handling.

### Batch Web Application (Milestones 2 and 3)

The FastAPI application and React dashboard are an initial local foundation. The existing CLI and Streamlit workflow remain separate. The web path stores batches, items, run status/events, immutable draft versions, exact-version review decisions, artifact references, and enqueue requests in the application database. Cover letters are generated before email drafts because the email writer uses the cover letter. The web worker does not call the legacy delivery graph.

Before starting the web app, install the Python requirements and configure `APP_DATABASE_URL` in the ignored `.env`. In local development/test with SQLite, the web app automatically applies migrations at startup. For PostgreSQL, apply migrations explicitly to a database you intentionally created:

```powershell
Copy-Item .env.example .env
# Edit .env locally; use APP_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/cold_outreach
psql -U postgres -c "CREATE DATABASE cold_outreach;"
python -m alembic upgrade head
```

The commands above are examples only. No PostgreSQL server or database is initialized automatically. For the durable worker, also configure a Redis instance and a private 16-, 24-, or 32-byte `LANGGRAPH_AES_KEY` for encrypted LangGraph checkpoints. Keep the key outside source control. Start the API with `uvicorn api:app --reload`; start Redis, the Celery worker, and Celery Beat separately from the repository root. On Windows, run these in separate terminals:

```powershell
celery -A webapp.queue.celery_app worker --pool=solo --loglevel=INFO
celery -A webapp.queue.celery_app beat --loglevel=INFO --schedule=runs/celerybeat-schedule
```

Create the `runs` directory before starting Beat if it does not exist. Celery imports `webapp.worker` at worker startup so both the scheduled outbox dispatcher and message processor are registered. The worker requires PostgreSQL; SQLite can persist API batches but cannot process research jobs. If the worker receives a queued job with SQLite configured, it records a visible `worker_requires_postgresql` failure for explicit retry after configuring the supported services.

The dashboard is in `frontend/`. Run `npm.cmd install` and `npm.cmd run dev` from that directory. Its Vite development proxy forwards `/api` and `/health` to `http://127.0.0.1:8000`. The frontend does not provide production sign-in. Protected routes fail closed unless a trusted authentication middleware supplies a principal. For local SQLite development/testing only, the fixed `X-Webapp-Test-Auth: enabled` identity is accepted when `APP_ENV=development` or `APP_ENV=test` and `WEBAPP_TEST_AUTH=true`. It cannot authenticate a production or PostgreSQL-backed app. Set `VITE_ENABLE_TEST_AUTH=true` in `frontend/.env.local` for local UI testing; never enable it in a deployed frontend.

The web application now supports owner-scoped Gmail web OAuth and explicit one-email delivery. Draft approval alone never sends. Tokens are encrypted at rest with the stable `LANGGRAPH_AES_KEY`, and only the Gmail `gmail.send` scope is requested. Live delivery remains disabled unless `GMAIL_TEST_MODE=false` and `GMAIL_LIVE_SEND_ENABLED=true`; test mode persists a `simulated` delivery and never calls Gmail. The additive `c3a8271f6d42` migration adds that test-only delivery status and widens encrypted token storage; apply migrations explicitly to PostgreSQL before enabling the web delivery feature. A delivery reservation is unique per batch item, so repeated clicks do not resend. If a reservation remains queued or sending for five minutes, check Gmail Sent and use **Check Sent and mark unknown** only when delivery cannot be confirmed. Unknown outcomes are never retried automatically. This is a local application workflow, not a production email platform; deploy only after appropriate security, consent, retention, operational recovery, and provider quota review.

#### Web dashboard Gmail setup

1. In Google Cloud Console, enable the Gmail API, configure the OAuth consent screen, and create an OAuth client ID with application type **Web application**. Add the exact callback URI below to the client's authorized redirect URIs. Add the local account as a test user if Google's consent screen is in testing.
2. In the ignored backend `.env`, configure `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/api/v1/gmail/oauth/callback`. The frontend origin must remain in `WEB_ALLOWED_ORIGINS`; the OAuth redirect is the backend callback.
3. Keep the existing stable `LANGGRAPH_AES_KEY` set. The API refuses to begin OAuth without a supported encryption key. Do not copy credentials or tokens into the frontend; no client secret or token is sent to browser JavaScript.
4. Apply `python -m alembic upgrade head` to the already-configured application database after verifying the target database. Do not run it against maintenance or unrelated databases.
5. Keep `GMAIL_TEST_MODE=true` and `GMAIL_LIVE_SEND_ENABLED=false` while connecting and checking the dashboard. Test-mode confirmation records a simulation only. To enable actual transmission later, deliberately set `GMAIL_TEST_MODE=false` and `GMAIL_LIVE_SEND_ENABLED=true`, restart the API, and verify the dashboard says live delivery is enabled.
6. For a safe one-email live check, select an eligible item whose contact address is a mailbox you control, review the saved subject/body and exact current approval, then use **Send Email** and confirm the displayed recipient, subject, and body. Send only one item. Confirm `sent` and its Gmail message ID in the dashboard; if the UI reports `unknown` or remains `sending`, check Gmail Sent and do not retry. Disconnecting explicitly revokes the Google token and removes its encrypted database value.

When Google OAuth is not configured, the dashboard connection action returns the names of missing settings without exposing secret values. The OAuth consent screen and redirect URI must be configured by the operator; the application does not invent or create credentials.

For a local dashboard/API smoke test without PostgreSQL, use a disposable SQLite file. This verifies protected API/UI requests and batch persistence only; **no worker runs on SQLite**, so batches remain queued and no research or draft generation occurs:

```powershell
# From the repository root in the API terminal:
New-Item -ItemType Directory -Force runs
$env:APP_ENV = "development"
$env:WEBAPP_TEST_AUTH = "true"
$env:APP_DATABASE_URL = "sqlite:///runs/web-review.sqlite3"
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

In a second terminal, enable only the local UI test header and start Vite:

```powershell
Push-Location frontend
npm.cmd install
npm.cmd run dev
Pop-Location
```

Open the Vite URL printed in the terminal. The local test identity and SQLite file are not suitable for deployment. A full durable worker run requires intentionally configured PostgreSQL, Redis, encrypted checkpoints, and a real trusted authentication integration, which is not implemented in this milestone.

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
