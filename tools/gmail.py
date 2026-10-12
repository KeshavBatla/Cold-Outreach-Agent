"""
Gmail API client integration for OAuth authentication, message construction, attachment handling, and sending.
"""

import os
import base64
import re
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
except ImportError:
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None

import config
from persistence import ensure_external_call_allowed, record_provider_usage


def get_gmail_service(token_path: Optional[str] = None):
    """
    Authenticate and build the Google Gmail API service using OAuth2 credentials.
    Reads token.json or initiates local server flow via credentials.json.
    Accepts an optional token_path, defaulting to config.TOKEN_FILE.
    """
    if Credentials is None or build is None:
        raise ImportError(
            "Google Gmail API dependencies are not installed. "
            "Please install google-api-python-client and google-auth-oauthlib."
        )

    creds = None
    resolved_token_path = Path(token_path) if token_path else Path(config.TOKEN_FILE)
    creds_path = Path(config.CREDENTIALS_FILE)

    if resolved_token_path.exists():
        creds = Credentials.from_authorized_user_file(str(resolved_token_path), config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif creds_path.exists():
            if InstalledAppFlow is None:
                raise ImportError("google_auth_oauthlib is required for OAuth login flow.")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            raise FileNotFoundError(
                f"Gmail credentials not found at {creds_path}. "
                "Download your OAuth client ID credentials.json from Google Cloud Console."
            )

        with open(resolved_token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def attach_file(message: MIMEMultipart, filepath: str) -> bool:
    """
    Attach a local file (e.g. PDF CV or cover letter) to a MIME multipart email.
    """
    if not filepath or not os.path.exists(filepath):
        return False
    try:
        with open(filepath, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename={os.path.basename(filepath)}",
        )
        message.attach(part)
        return True
    except Exception as e:
        print(f"[attach_file] Error attaching file {filepath}: {e}")
        return False


def send_email(
    to_email: str,
    subject: str,
    body: str,
    attachment_paths: Optional[List[str]] = None,
    dry_run: bool = False,
    approved: bool = False,
    run_id: str = "gmail",
) -> Dict[str, Any]:
    """
    Construct and send an email via Gmail API, or simulate in dry-run mode.
    """
    normalized_email = (to_email or "").strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
        return {"sent": False, "delivery_status": "failed", "error": "Invalid recipient email address"}
    if not approved:
        return {
            "sent": False,
            "delivery_status": "failed",
            "error": "Email dispatch requires explicit human approval.",
        }

    missing = [path for path in (attachment_paths or []) if path and not os.path.isfile(path)]
    if missing:
        return {
            "sent": False,
            "delivery_status": "failed",
            "error": "Missing attachment(s): " + ", ".join(missing),
        }

    if config.MOCK_MODE:
        failed = config.MOCK_SCENARIO == "failed_send"
        return {
            "sent": False,
            "simulated": not failed,
            "delivery_status": "failed" if failed else "simulated",
            "error": "Simulated Gmail send failure" if failed else None,
            "message_id": "mock-message-id" if not failed else None,
        }

    if dry_run or config.DRY_RUN:
        print(f"\n[DRY RUN] Would send to: {to_email}")
        print(f"Subject: {subject}")
        print(f"Attachments: {attachment_paths}")
        print(f"Body:\n{body}\n")
        return {"sent": False, "dry_run": True, "delivery_status": "dry_run", "message_id": "dry-run-preview"}

    ensure_external_call_allowed(run_id)
    message = MIMEMultipart()
    message["to"] = normalized_email
    message["subject"] = subject
    message.attach(MIMEText(body, "plain"))

    try:
        service = get_gmail_service()
    except Exception as e:
        record_provider_usage(
            run_id=run_id,
            provider="gmail",
            operation="authentication",
            successful=False,
            error=str(e),
        )
        return {"sent": False, "dry_run": False, "delivery_status": "failed", "error": str(e)}

    if attachment_paths:
        for path in attachment_paths:
            if path and not attach_file(message, path):
                return {
                    "sent": False,
                    "dry_run": False,
                    "delivery_status": "failed",
                    "error": f"Could not attach required file: {path}",
                }

    started = time.monotonic()
    try:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        message_id = sent.get("id") if isinstance(sent, dict) else None
        if not message_id:
            record_provider_usage(
                run_id=run_id,
                provider="gmail",
                operation="send",
                successful=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error="Gmail returned no message ID; delivery is ambiguous.",
            )
            return {
                "sent": False,
                "dry_run": False,
                "delivery_status": "unknown",
                "error": "Gmail returned no message ID; delivery is ambiguous.",
            }
        record_provider_usage(
            run_id=run_id,
            provider="gmail",
            operation="send",
            successful=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "sent": True,
            "dry_run": False,
            "delivery_status": "sent",
            "message_id": message_id,
        }
    except Exception as e:
        print(f"[send_email] Error sending email: {e}")
        record_provider_usage(
            run_id=run_id,
            provider="gmail",
            operation="send",
            successful=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=str(e),
        )
        return {
            "sent": False,
            "dry_run": False,
            "delivery_status": "unknown",
            "error": str(e),
        }
