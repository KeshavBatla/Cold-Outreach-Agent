"""
Gmail API client integration for OAuth authentication, message construction, attachment handling, and sending.
"""

import os
import base64
from pathlib import Path
from typing import List, Optional, Dict, Any
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config


def get_gmail_service():
    """
    Authenticate and build the Google Gmail API service using OAuth2 credentials.
    Reads token.json or initiates local server flow via credentials.json.
    """
    creds = None
    token_path = Path(config.TOKEN_FILE)
    creds_path = Path(config.CREDENTIALS_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif creds_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            raise FileNotFoundError(
                f"Gmail credentials not found at {creds_path}. "
                "Download your OAuth client ID credentials.json from Google Cloud Console."
            )

        with open(token_path, "w", encoding="utf-8") as token_file:
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
) -> Dict[str, Any]:
    """
    Construct and send an email via Gmail API, or simulate in dry-run mode.
    """
    if not to_email:
        return {"sent": False, "error": "No recipient email provided"}

    if dry_run or config.DRY_RUN:
        print(f"\n[DRY RUN] Would send to: {to_email}")
        print(f"Subject: {subject}")
        print(f"Attachments: {attachment_paths}")
        print(f"Body:\n{body}\n")
        return {"sent": False, "dry_run": True, "message_id": "dry-run-preview"}

    message = MIMEMultipart()
    message["to"] = to_email
    message["subject"] = subject
    message.attach(MIMEText(body, "plain"))

    if attachment_paths:
        for path in attachment_paths:
            if path:
                attached = attach_file(message, path)
                if not attached:
                    print(f"[send_email] Warning: Could not attach {path}")

    try:
        service = get_gmail_service()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"sent": True, "dry_run": False, "message_id": sent.get("id")}
    except Exception as e:
        print(f"[send_email] Error sending email: {e}")
        return {"sent": False, "dry_run": False, "error": str(e)}

