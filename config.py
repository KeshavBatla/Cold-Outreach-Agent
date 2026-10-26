"""
Configuration module for the Cold Outreach Multi-Agent System.
Handles environment variable loading, model initialization, and default paths.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI

# Load environment variables from .env file
load_dotenv()

# Base project paths
PROJECT_ROOT = Path(__file__).resolve().parent
RUNS_DIR = PROJECT_ROOT / "runs"
COVER_LETTER_DIR = PROJECT_ROOT
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

# Typography and fonts
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"


def get_run_dir(thread_id: str) -> Path:
    """
    Ensure and return an isolated runs directory for a specific thread_id.
    """
    run_dir = RUNS_DIR / str(thread_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "models/gemini-3.1-flash-lite")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY") or os.getenv("HUNTER_API")

# System & Execution Flags
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes")
MOCK_SCENARIO = os.getenv("MOCK_SCENARIO", "good").strip().lower()
DATABASE_URL = (
    os.getenv("APP_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or f"sqlite:///{(RUNS_DIR / 'outreach.sqlite3').as_posix()}"
)
LOCAL_OWNER_ID = os.getenv("OUTREACH_OWNER_ID", "local")
DUPLICATE_COOLDOWN_DAYS = int(os.getenv("DUPLICATE_COOLDOWN_DAYS", "30"))
RESEARCH_CACHE_TTL_DAYS = int(os.getenv("RESEARCH_CACHE_TTL_DAYS", "30"))
VERIFICATION_FRESHNESS_DAYS = int(os.getenv("VERIFICATION_FRESHNESS_DAYS", "30"))
MAX_CONTACTS_PER_COMPANY = int(os.getenv("MAX_CONTACTS_PER_COMPANY", "5"))
MAX_EMAIL_VERIFICATIONS = int(os.getenv("MAX_EMAIL_VERIFICATIONS", "3"))
MAX_EXTERNAL_CALLS_PER_RUN = int(os.getenv("MAX_EXTERNAL_CALLS_PER_RUN", "20"))
MAX_STAGE_ATTEMPTS = 2
MAX_RETRIES = 3
SPECULATIVE_SEARCH_BUDGET = max(1, min(int(os.getenv("SPECULATIVE_SEARCH_BUDGET", "10")), 50))

# Gmail API Scopes (Send-only for security)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def get_llm(model: str = None, temperature: float = 0.2) -> ChatGoogleGenerativeAI:
    """
    Factory function to instantiate the Google Generative AI LLM client.
    """
    if MOCK_MODE:
        raise RuntimeError(
            "MOCK_MODE is enabled; agent mock fixtures must be used instead of creating a Gemini client."
        )
    api_key = GEMINI_API_KEY
    if not api_key:
        raise ValueError(
            "Missing Gemini API key. Please set GEMINI_API_KEY or GOOGLE_API_KEY in your .env file."
        )
    return ChatGoogleGenerativeAI(
        model=model or MODEL_NAME,
        google_api_key=api_key,
        temperature=temperature
    )

# Maximum speculative research attempts before finishing gracefully
MAX_RESEARCH_ATTEMPTS = max(1, min(int(os.getenv("MAX_RESEARCH_ATTEMPTS", "5")), 20))
