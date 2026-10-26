from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import config
from webapp.settings import Settings


class GmailConfigurationError(RuntimeError):
    pass


class GmailTokenError(RuntimeError):
    pass


def missing_oauth_settings(settings: Settings) -> list[str]:
    required = {
        "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": settings.google_oauth_client_secret,
        "GOOGLE_OAUTH_REDIRECT_URI": settings.google_oauth_redirect_uri,
    }
    return [name for name, value in required.items() if not value]


def _encryption_key() -> bytes:
    value = os.getenv("LANGGRAPH_AES_KEY")
    if value is None or len(value.encode("utf-8")) not in {16, 24, 32}:
        raise GmailConfigurationError(
            "Set LANGGRAPH_AES_KEY to the stable 16, 24, or 32 byte key already used by the workflow checkpointer."
        )
    return hashlib.sha256(b"cold-outreach-gmail-token-v1\0" + value.encode("utf-8")).digest()


def validate_token_encryption_config() -> None:
    _encryption_key()


def make_oauth_state_cookie(state: str, frontend_origin: str) -> str:
    encoded_origin = base64.urlsafe_b64encode(frontend_origin.encode("utf-8")).decode("ascii").rstrip("=")
    payload = f"{state}.{encoded_origin}"
    signature = hmac.new(_encryption_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_oauth_state_cookie(cookie_value: str, state: str) -> str | None:
    try:
        cookie_state, encoded_origin, signature = cookie_value.split(".", 2)
        payload = f"{cookie_state}.{encoded_origin}"
        expected = hmac.new(_encryption_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(cookie_state, state) or not hmac.compare_digest(signature, expected):
            return None
        padded = encoded_origin + "=" * (-len(encoded_origin) % 4)
        origin = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        return origin
    except (UnicodeDecodeError, ValueError, GmailConfigurationError):
        return None


def encrypt_token(owner_id: str, connection_id: str, token_json: str) -> str:
    cipher = AES.new(_encryption_key(), AES.MODE_GCM)
    cipher.update(f"gmail-token:{owner_id}:{connection_id}".encode("utf-8"))
    ciphertext, tag = cipher.encrypt_and_digest(token_json.encode("utf-8"))
    packed = cipher.nonce + tag + ciphertext
    return "v1." + base64.urlsafe_b64encode(packed).decode("ascii")


def decrypt_token(owner_id: str, connection_id: str, encrypted: str) -> str:
    try:
        version, encoded = encrypted.split(".", 1)
        if version != "v1":
            raise ValueError("Unsupported token version.")
        packed = base64.urlsafe_b64decode(encoded.encode("ascii"))
        nonce, tag, ciphertext = packed[:16], packed[16:32], packed[32:]
        cipher = AES.new(_encryption_key(), AES.MODE_GCM, nonce=nonce)
        cipher.update(f"gmail-token:{owner_id}:{connection_id}".encode("utf-8"))
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except GmailConfigurationError:
        raise
    except Exception as error:
        raise GmailTokenError("Stored Gmail credentials could not be decrypted.") from error

def create_oauth_flow(
    settings: Settings,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    missing = missing_oauth_settings(settings)
    if missing:
        raise GmailConfigurationError(
            "Configure Google OAuth web-client settings: "
            + ", ".join(missing)
            + "."
        )

    client_config = {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=config.GMAIL_SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=False,
    )

    flow.redirect_uri = settings.google_oauth_redirect_uri
    return flow

def credentials_from_token(token_json: str) -> Credentials:
    try:
        credentials = Credentials.from_authorized_user_info(
            json.loads(token_json),
            scopes=config.GMAIL_SCOPES,
        )
    except Exception as error:
        raise GmailTokenError("Stored Gmail credentials are invalid.") from error
    if not credentials.refresh_token:
        raise GmailTokenError("Gmail authorization did not provide an offline refresh token.")
    return credentials


def build_gmail_service(credentials: Credentials) -> Any:
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def build_mime_message(
    recipient: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> EmailMessage:
    """
    Build a standard MIME email with attachments using email.message.EmailMessage.
    Supports PDF and DOCX attachments with appropriate MIME types and sanitized dynamic filenames.
    """
    if not recipient or not recipient.strip():
        raise ValueError("Recipient address is required.")
    if not subject or not subject.strip():
        raise ValueError("Email subject is required.")
    if not body or not body.strip():
        raise ValueError("Email body is required.")

    message = EmailMessage()
    message["To"] = recipient.strip()
    message["Subject"] = subject.strip()
    message.set_content(body.strip())

    seen_filenames: set[str] = set()
    for att in attachments or []:
        filename = att.get("filename")
        content = att.get("content")
        if not filename or not content:
            continue
        safe_filename = Path(filename).name
        if safe_filename in seen_filenames:
            continue
        seen_filenames.add(safe_filename)

        mimetype = att.get("mimetype")
        if not mimetype:
            mimetype, _ = mimetypes.guess_type(safe_filename)
        if not mimetype:
            mimetype = "application/octet-stream"

        if "/" in mimetype:
            maintype, subtype = mimetype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=safe_filename,
        )

    return message


def execute_gmail_send(
    service: Any,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> str | None:
    message = build_mime_message(recipient, subject, body, attachments=attachments)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute(num_retries=0)
    return result.get("id") if isinstance(result, dict) else None


def refresh_credentials(credentials: Credentials) -> None:
    if not credentials.valid or credentials.expired:
        credentials.refresh(Request())
