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
COVER_LETTER_DIR = PROJECT_ROOT
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"
FONT_REGULAR = PROJECT_ROOT / "DejaVuSans.ttf"
FONT_BOLD = PROJECT_ROOT / "DejaVuSans-Bold.ttf"

# Resolve default CV / resume path
if (PROJECT_ROOT / "Keshav_CV_3rd.pdf").exists():
    DEFAULT_RESUME_PATH = str(PROJECT_ROOT / "Keshav_CV_3rd.pdf")
else:
    DEFAULT_RESUME_PATH = str(PROJECT_ROOT / "resume.pdf")

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "models/gemini-2.5-flash")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY") or os.getenv("HUNTER_API")

# System & Execution Flags
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
MAX_STAGE_ATTEMPTS = 2
MAX_RETRIES = 3

# Gmail API Scopes (Send-only for security)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def get_llm(model: str = None, temperature: float = 0.2) -> ChatGoogleGenerativeAI:
    """
    Factory function to instantiate the Google Generative AI LLM client.
    """
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

