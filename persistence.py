"""SQLAlchemy-backed research, cache, exclusion, and outreach history."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    func,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

import config


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalize_company_name(value: Optional[str]) -> str:
    text = (value or "").casefold()
    text = re.sub(r"\b(incorporated|corporation|limited|llc|ltd|inc|corp|plc|gmbh)\b", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def normalize_domain(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip().casefold()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_email(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip().casefold()
    return text or None


def company_key(name: Optional[str], domain: Optional[str] = None) -> str:
    canonical_domain = normalize_domain(domain)
    if canonical_domain:
        return f"domain:{canonical_domain}"
    normalized_name = normalize_company_name(name)
    return f"name:{normalized_name}" if normalized_name else ""


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    canonical_domain: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    last_researched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("owner_id", "company_id", "normalized_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(160), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[Optional[str]] = mapped_column(String(300))
    normalized_email: Mapped[str] = mapped_column(String(320), index=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(300))
    source_provider: Mapped[Optional[str]] = mapped_column(String(80))
    verification_status: Mapped[str] = mapped_column(String(40), default="unknown")
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    company: Mapped[Company] = relationship(back_populates="contacts")


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(300))
    role: Mapped[Optional[str]] = mapped_column(String(300))
    outcome: Mapped[str] = mapped_column(String(50))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    provider_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class OutreachEvent(Base):
    __tablename__ = "outreach_events"
    __table_args__ = (
        Index("ix_outreach_sent_email_time", "normalized_email", "sent_at"),
        Index("ix_outreach_sent_company_time", "company_key", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contacts.id"), index=True)
    company_key: Mapped[str] = mapped_column(String(400), index=True)
    normalized_company_name: Mapped[str] = mapped_column(String(300), index=True)
    recipient_email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320), index=True)
    delivery_status: Mapped[str] = mapped_column(String(40), index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(300))
    error_information: Mapped[Optional[str]] = mapped_column(Text)
    reservation_token: Mapped[Optional[str]] = mapped_column(String(36), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Exclusion(Base):
    __tablename__ = "exclusions"
    __table_args__ = (UniqueConstraint("owner_id", "normalized_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(160), index=True)
    normalized_key: Mapped[str] = mapped_column(String(400), index=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ResearchCache(Base):
    __tablename__ = "research_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    cached_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ReservationGuard(Base):
    __tablename__ = "reservation_guards"

    guard_key: Mapped[str] = mapped_column(String(450), primary_key=True)
    token: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    operation: Mapped[str] = mapped_column(String(120))
    model: Mapped[Optional[str]] = mapped_column(String(160))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    successful: Mapped[bool] = mapped_column(Boolean)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    estimated_cost: Mapped[Optional[float]] = mapped_column(Float)
    cache_hit: Mapped[Optional[bool]] = mapped_column(Boolean)
    error_information: Mapped[Optional[str]] = mapped_column(Text)


class AppUser(Base):
    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("identity_provider", "external_subject", name="uq_app_user_external_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class LocalCredential(Base):
    __tablename__ = "local_credentials"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    login_name: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class WebAuthSession(Base):
    __tablename__ = "web_auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class GmailConnection(Base):
    __tablename__ = "gmail_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, unique=True)
    account_email: Mapped[Optional[str]] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(24), default="unlinked", nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    token_secret_ref: Mapped[Optional[str]] = mapped_column(Text)
    linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('unlinked', 'connected', 'revoked')", name="ck_gmail_connection_status"),
    )


class OutreachBatch(Base):
    __tablename__ = "outreach_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[Optional[str]] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_outreach_batches_id_owner"),
        CheckConstraint(
            "status IN ('queued', 'researching', 'research_review', 'drafting', 'draft_review', 'completed', 'failed')",
            name="ck_outreach_batch_status",
        ),
    )


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "owner_id"],
            ["outreach_batches.id", "outreach_batches.owner_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "owner_id", name="uq_batch_items_id_owner"),
        UniqueConstraint("id", "owner_id", "batch_id", name="uq_batch_items_id_owner_batch"),
        CheckConstraint(
            "status IN ('queued', 'researching', 'research_review', 'rejected', 'ineligible', 'drafting', 'draft_review', 'failed', 'completed')",
            name="ck_batch_item_status",
        ),
        CheckConstraint(
            "research_decision IS NULL OR research_decision IN ('approved', 'rejected')",
            name="ck_batch_item_research_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(24), default="open_role", nullable=False)
    target_company: Mapped[Optional[str]] = mapped_column(String(300))
    target_domain: Mapped[Optional[str]] = mapped_column(String(255))
    target_role: Mapped[Optional[str]] = mapped_column(String(300))
    target_description: Mapped[Optional[str]] = mapped_column(Text)
    candidate_name: Mapped[Optional[str]] = mapped_column(String(200))
    candidate_profile: Mapped[Optional[str]] = mapped_column(Text)
    target_seniority: Mapped[Optional[str]] = mapped_column(String(200))
    target_domain_keywords: Mapped[Optional[str]] = mapped_column(String(500))
    location_preference: Mapped[Optional[str]] = mapped_column(String(300))
    cv_artifact_id: Mapped[Optional[str]] = mapped_column(ForeignKey("private_artifacts.id", ondelete="SET NULL"))
    example_letter_artifact_id: Mapped[Optional[str]] = mapped_column(ForeignKey("private_artifacts.id", ondelete="SET NULL"))
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    research_decision: Mapped[Optional[str]] = mapped_column(String(16))
    research_result: Mapped[Optional[dict]] = mapped_column(JSON)
    failure_code: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class ApplicationRun(Base):
    __tablename__ = "application_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "owner_id", "batch_id"],
            ["batch_items.id", "batch_items.owner_id", "batch_items.batch_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("thread_id", name="uq_application_runs_thread_id"),
        UniqueConstraint("id", "owner_id", name="uq_application_runs_id_owner"),
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_application_run_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    phase: Mapped[Optional[str]] = mapped_column(String(48))
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    safe_error_code: Mapped[Optional[str]] = mapped_column(String(80))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "owner_id"],
            ["outreach_batches.id", "outreach_batches.owner_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("batch_id", "sequence", name="uq_run_events_batch_sequence"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_run_event_progress"),
        Index("ix_run_events_batch_sequence", "batch_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("application_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    phase: Mapped[Optional[str]] = mapped_column(String(48))
    status: Mapped[Optional[str]] = mapped_column(String(24))
    progress: Mapped[Optional[int]] = mapped_column(Integer)
    item_id: Mapped[Optional[str]] = mapped_column(String(36))
    safe_detail: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False, index=True)


class PrivateArtifact(Base):
    __tablename__ = "private_artifacts"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_private_artifacts_storage_key"),
        CheckConstraint(
            "artifact_type IN ('cv', 'example_letter', 'cover_letter', 'email_attachment')",
            name="ck_private_artifact_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    item_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(255))
    media_type: Mapped[Optional[str]] = mapped_column(String(120))
    byte_size: Mapped[Optional[int]] = mapped_column(Integer)
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class DraftVersion(Base):
    __tablename__ = "draft_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "owner_id", "batch_id"],
            ["batch_items.id", "batch_items.owner_id", "batch_items.batch_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("item_id", "artifact_type", "version", name="uq_draft_item_artifact_version"),
        UniqueConstraint(
            "id", "owner_id", "item_id", "artifact_type",
            name="uq_draft_version_approval_target",
        ),
        CheckConstraint("artifact_type IN ('cover_letter', 'email')", name="ck_draft_artifact_type"),
        CheckConstraint("version > 0", name="ck_draft_version_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(24), default="agent", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "owner_id", "batch_id"],
            ["batch_items.id", "batch_items.owner_id", "batch_items.batch_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["draft_version_id", "owner_id", "item_id", "artifact_type"],
            [
                "draft_versions.id",
                "draft_versions.owner_id",
                "draft_versions.item_id",
                "draft_versions.artifact_type",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "item_id", "artifact_type", "draft_version_id",
            name="uq_approval_exact_draft_decision",
        ),
        UniqueConstraint(
            "owner_id", "item_id", "artifact_type", "draft_version_id", "decision",
            name="uq_approval_delivery_reference",
        ),
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_approval_decision"),
        CheckConstraint("artifact_type IN ('cover_letter', 'email')", name="ck_approval_artifact_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(24), nullable=False)
    draft_version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        Index("ix_outbox_pending", "published_at", "available_at", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    processing_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class DeliveryRecord(Base):
    __tablename__ = "delivery_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "owner_id", "batch_id"],
            ["batch_items.id", "batch_items.owner_id", "batch_items.batch_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["draft_version_id", "owner_id", "item_id", "artifact_type"],
            [
                "draft_versions.id",
                "draft_versions.owner_id",
                "draft_versions.item_id",
                "draft_versions.artifact_type",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "item_id", "artifact_type", "draft_version_id", "approval_decision"],
            [
                "approval_decisions.owner_id",
                "approval_decisions.item_id",
                "approval_decisions.artifact_type",
                "approval_decisions.draft_version_id",
                "approval_decisions.decision",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_key", name="uq_delivery_idempotency_key"),
        CheckConstraint("approval_decision = 'approved'", name="ck_delivery_requires_approval"),
        CheckConstraint("artifact_type = 'email'", name="ck_delivery_email_artifact"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown', 'cancelled', 'simulated')",
            name="ck_delivery_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(24), default="email", nullable=False)
    draft_version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    approval_decision: Mapped[str] = mapped_column(String(16), default="approved", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(300))
    safe_error_code: Mapped[Optional[str]] = mapped_column(String(80))
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


_engine_lock = threading.Lock()
_engines: dict[str, Any] = {}


@lru_cache(maxsize=8)
def _engine_for(database_url: str):
    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        sqlite_path = database_url.removeprefix("sqlite:///")
        Path(sqlite_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(database_url, **options)
    if database_url.startswith("sqlite:"):
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            try:
                from sqlalchemy import text
                connection.execute(text("ALTER TABLE outreach_batches ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0"))
            except Exception:
                pass
    _engines[database_url] = engine
    return engine


def get_engine(database_url: Optional[str] = None):
    """Return the configured database engine and initialize the schema reproducibly."""
    url = database_url or config.DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    with _engine_lock:
        return _engine_for(url)


def _target_keys(name: Optional[str], domain: Optional[str]) -> tuple[str, str, list[str]]:
    normalized_name = normalize_company_name(name)
    canonical_domain = normalize_domain(domain)
    keys = [f"name:{normalized_name}"] if normalized_name else []
    if canonical_domain:
        keys.append(f"domain:{canonical_domain}")
    return normalized_name, canonical_domain or "", keys


def _identity_keys(normalized_name: str, canonical_domain: str) -> list[str]:
    if canonical_domain:
        return [f"domain:{canonical_domain}"]
    return [f"name:{normalized_name}"] if normalized_name else []


def _company_row(session: Session, name: Optional[str], domain: Optional[str]) -> Optional[Company]:
    normalized_name, canonical_domain, _ = _target_keys(name, domain)
    company = None
    if canonical_domain:
        company = session.scalar(select(Company).where(Company.canonical_domain == canonical_domain))
    elif normalized_name:
        company = session.scalar(select(Company).where(Company.normalized_name == normalized_name))
    if company is None and (normalized_name or canonical_domain):
        company = Company(normalized_name=normalized_name or canonical_domain, canonical_domain=canonical_domain or None)
        session.add(company)
        session.flush()
    elif company and canonical_domain and not company.canonical_domain:
        company.canonical_domain = canonical_domain
    return company


def record_research_run(
    run_id: str,
    company_name: Optional[str],
    role: Optional[str],
    outcome: str,
    *,
    owner_id: Optional[str] = None,
    domain: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    source_urls: Optional[list[str]] = None,
    provider_usage: Optional[dict[str, Any]] = None,
) -> None:
    with Session(get_engine()) as session, session.begin():
        company = _company_row(session, company_name, domain)
        if company:
            company.last_researched_at = utc_now()
        session.add(ResearchRun(
            run_id=run_id,
            owner_id=owner_id or config.LOCAL_OWNER_ID,
            company_id=company.id if company else None,
            company_name=company_name,
            role=role,
            outcome=outcome,
            rejection_reason=rejection_reason,
            source_urls=source_urls or [],
            provider_usage=provider_usage or {},
        ))


def save_contacts(
    company_name: str,
    domain: Optional[str],
    candidates: list[dict[str, Any]],
    *,
    verification_status: str = "unknown",
    owner_id: Optional[str] = None,
) -> None:
    contact_owner = owner_id or config.LOCAL_OWNER_ID
    with Session(get_engine()) as session, session.begin():
        company = _company_row(session, company_name, domain)
        if company is None:
            return
        now = utc_now()
        for candidate in candidates:
            email = normalize_email(candidate.get("email"))
            if not email:
                continue
            contact = session.scalar(select(Contact).where(
                Contact.company_id == company.id,
                Contact.owner_id == contact_owner,
                Contact.normalized_email == email,
            ))
            if contact is None:
                contact = Contact(
                    owner_id=contact_owner,
                    company_id=company.id,
                    normalized_email=email,
                    created_at=now,
                )
                session.add(contact)
            contact.name = candidate.get("name")
            contact.job_title = candidate.get("position") or candidate.get("job_title")
            contact.source_provider = candidate.get("source_provider", "hunter")
            contact.verification_status = verification_status
            contact.last_verified_at = now if verification_status in {"valid", "invalid"} else None
            contact.updated_at = now


def get_cached_contacts(
    company_name: Optional[str],
    domain: Optional[str],
    *,
    ttl_days: Optional[int] = None,
    run_id: str = "cache",
    owner_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    normalized_name, canonical_domain, _ = _target_keys(company_name, domain)
    with Session(get_engine()) as session:
        company = None
        if canonical_domain:
            company = session.scalar(select(Company).where(Company.canonical_domain == canonical_domain))
        elif normalized_name:
            company = session.scalar(select(Company).where(Company.normalized_name == normalized_name))
        if company is None:
            record_provider_usage(
                run_id=run_id,
                provider="cache",
                operation="contact_lookup",
                successful=True,
                cache_hit=False,
            )
            return []
        cutoff = utc_now() - timedelta(days=ttl_days or config.RESEARCH_CACHE_TTL_DAYS)
        contacts = list(session.scalars(select(Contact).where(
            Contact.company_id == company.id,
            Contact.owner_id == (owner_id or config.LOCAL_OWNER_ID),
            Contact.updated_at >= cutoff,
        )))
        record_provider_usage(
            run_id=run_id,
            provider="cache",
            operation="contact_lookup",
            successful=True,
            cache_hit=bool(contacts),
        )
        return [
            {
                "name": contact.name,
                "email": contact.normalized_email,
                "position": contact.job_title,
                "verification_status": (
                    contact.verification_status if is_contact_verification_fresh(contact) else "stale"
                ),
            }
            for contact in contacts
        ]


def update_contact_verification(
    company_name: str,
    domain: Optional[str],
    email: str,
    valid: bool,
    *,
    owner_id: Optional[str] = None,
) -> None:
    normalized_email = normalize_email(email)
    normalized_name, canonical_domain, _ = _target_keys(company_name, domain)
    with Session(get_engine()) as session, session.begin():
        company = None
        if canonical_domain:
            company = session.scalar(select(Company).where(Company.canonical_domain == canonical_domain))
        elif normalized_name:
            company = session.scalar(select(Company).where(Company.normalized_name == normalized_name))
        if company is None or normalized_email is None:
            return
        contact = session.scalar(select(Contact).where(
            Contact.company_id == company.id,
            Contact.owner_id == (owner_id or config.LOCAL_OWNER_ID),
            Contact.normalized_email == normalized_email,
        ))
        if contact:
            contact.verification_status = "valid" if valid else "invalid"
            contact.last_verified_at = utc_now()
            contact.updated_at = utc_now()


def company_block_reason(
    company_name: Optional[str],
    domain: Optional[str],
    *,
    owner_id: Optional[str] = None,
    cooldown_days: Optional[int] = None,
) -> Optional[str]:
    normalized_name, canonical_domain, _ = _target_keys(company_name, domain)
    identities = _identity_keys(normalized_name, canonical_domain)
    if not identities:
        return "A company name or canonical domain is required."
    cutoff = utc_now() - timedelta(days=cooldown_days or config.DUPLICATE_COOLDOWN_DAYS)
    with Session(get_engine()) as session:
        if owner_id and session.scalar(select(Exclusion.id).where(
            Exclusion.owner_id == owner_id,
            Exclusion.normalized_key.in_(identities),
        ).limit(1)):
            return "This company is excluded for this owner."
        if canonical_domain:
            conditions = [
                OutreachEvent.company_key == f"domain:{canonical_domain}",
                OutreachEvent.company_key == f"name:{normalized_name}",
            ] if normalized_name else [OutreachEvent.company_key == f"domain:{canonical_domain}"]
        else:
            conditions = [
                OutreachEvent.company_key == f"name:{normalized_name}",
                OutreachEvent.normalized_company_name == normalized_name,
            ]
        sent = session.scalar(select(OutreachEvent.id).where(
            OutreachEvent.delivery_status == "sent",
            OutreachEvent.sent_at >= cutoff,
            or_(*conditions),
        ).limit(1))
        if sent:
            return "Recently contacted through this service."
    return None


def get_cached_research(cache_key: str, *, run_id: str = "cache") -> Optional[dict[str, Any]]:
    with Session(get_engine()) as session:
        item = session.get(ResearchCache, cache_key)
        hit = bool(item and _aware(item.expires_at) > _aware(utc_now()))
        if not hit:
            if item:
                session.delete(item)
                session.commit()
            record_provider_usage(
                run_id=run_id,
                provider="cache",
                operation="research_lookup",
                successful=True,
                cache_hit=False,
            )
            return None
        payload = dict(item.payload)
        record_provider_usage(
            run_id=run_id,
            provider="cache",
            operation="research_lookup",
            successful=True,
            cache_hit=True,
        )
        return payload


def set_cached_research(cache_key: str, payload: dict[str, Any], ttl_days: Optional[int] = None) -> None:
    now = utc_now()
    with Session(get_engine()) as session, session.begin():
        item = session.get(ResearchCache, cache_key)
        if item is None:
            item = ResearchCache(cache_key=cache_key, payload=payload, cached_at=now)
            session.add(item)
        item.payload = payload
        item.cached_at = now
        item.expires_at = now + timedelta(days=ttl_days or config.RESEARCH_CACHE_TTL_DAYS)


def research_cache_key(
    company_name: Optional[str],
    role: Optional[str],
    seniority: Optional[str],
    location: Optional[str],
    domain_keywords: Optional[str],
) -> str:
    value = {
        "company": normalize_company_name(company_name),
        "role": (role or "").strip().casefold(),
        "seniority": (seniority or "").strip().casefold(),
        "location": (location or "").strip().casefold(),
        "domain_keywords": (domain_keywords or "").strip().casefold(),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def get_exclusions(owner_id: str) -> list[dict[str, Any]]:
    if not owner_id:
        return []
    with Session(get_engine()) as session:
        exclusions = session.scalars(
            select(Exclusion).where(Exclusion.owner_id == owner_id)
        ).all()
        results = []
        for exc in exclusions:
            raw_key = exc.normalized_key or ""
            if raw_key.startswith("domain:"):
                domain = raw_key.removeprefix("domain:")
                company_name = None
            elif raw_key.startswith("name:"):
                domain = None
                company_name = raw_key.removeprefix("name:")
            else:
                domain = None
                company_name = raw_key
            results.append({
                "normalized_key": raw_key,
                "company_name": company_name,
                "company_domain": domain,
                "rejection_reason": exc.rejection_reason or "Previously excluded",
                "created_at": exc.created_at,
            })
        return results


def add_exclusion(owner_id: str, company_name: Optional[str], domain: Optional[str], reason: Optional[str] = None) -> None:
    normalized_name, canonical_domain, _ = _target_keys(company_name, domain)
    _, _, keys = _target_keys(company_name, domain)
    if not keys:
        raise ValueError("A company name or canonical domain is required for an exclusion.")
    with Session(get_engine()) as session, session.begin():
        for key in keys:
            existing = session.scalar(select(Exclusion).where(
                Exclusion.owner_id == owner_id,
                Exclusion.normalized_key == key,
            ))
            if existing is None:
                session.add(Exclusion(owner_id=owner_id, normalized_key=key, rejection_reason=reason))
            elif reason and not existing.rejection_reason:
                existing.rejection_reason = reason


def outreach_block_reason(
    company_name: Optional[str],
    domain: Optional[str],
    recipient_email: Optional[str],
    *,
    owner_id: Optional[str] = None,
    cooldown_days: Optional[int] = None,
) -> Optional[str]:
    normalized_name, canonical_domain, _ = _target_keys(company_name, domain)
    identities = _identity_keys(normalized_name, canonical_domain)
    email = normalize_email(recipient_email)
    if not normalized_name and not canonical_domain:
        return "A company name or canonical domain is required."
    if not email:
        return "A valid recipient email address is required."
    cutoff = utc_now() - timedelta(days=cooldown_days or config.DUPLICATE_COOLDOWN_DAYS)
    with Session(get_engine()) as session:
        if owner_id:
            excluded = session.scalar(select(Exclusion.id).where(
                Exclusion.owner_id == owner_id,
                Exclusion.normalized_key.in_(identities),
            ).limit(1))
            if excluded:
                return "This company is excluded for this owner."
        company_conditions = (
            [
                OutreachEvent.company_key == f"domain:{canonical_domain}",
                OutreachEvent.company_key == f"name:{normalized_name}",
            ]
            if canonical_domain and normalized_name
            else [OutreachEvent.company_key == f"domain:{canonical_domain}"]
            if canonical_domain
            else [
                OutreachEvent.company_key == f"name:{normalized_name}",
                OutreachEvent.normalized_company_name == normalized_name,
            ]
        )
        sent = session.scalar(select(OutreachEvent.id).where(
            OutreachEvent.delivery_status == "sent",
            OutreachEvent.sent_at >= cutoff,
            or_(OutreachEvent.normalized_email == email, *company_conditions),
        ).limit(1))
        if sent:
            return "Recently contacted through this service."
    return None


def reserve_outreach(
    run_id: str,
    company_name: Optional[str],
    domain: Optional[str],
    recipient_email: Optional[str],
    *,
    owner_id: Optional[str] = None,
    cooldown_days: Optional[int] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Atomically check exclusions/history and reserve company and recipient guards."""
    normalized_name, canonical_domain, keys = _target_keys(company_name, domain)
    identities = _identity_keys(normalized_name, canonical_domain)
    email = normalize_email(recipient_email)
    if not normalized_name and not canonical_domain:
        return None, "A company name or canonical domain is required."
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None, "A valid recipient email address is required."
    cutoff = utc_now() - timedelta(days=cooldown_days or config.DUPLICATE_COOLDOWN_DAYS)
    token = str(uuid.uuid4())
    guard_keys = sorted({*[f"company:{key}" for key in identities], f"email:{email}"})
    with Session(get_engine()) as session:
        try:
            with session.begin():
                if session.get_bind().dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                for guard_key in guard_keys:
                    try:
                        with session.begin_nested():
                            session.add(ReservationGuard(guard_key=guard_key))
                            session.flush()
                    except IntegrityError:
                        pass
                guards = list(session.scalars(
                    select(ReservationGuard)
                    .where(ReservationGuard.guard_key.in_(guard_keys))
                    .order_by(ReservationGuard.guard_key)
                    .with_for_update()
                ))
                now = utc_now()
                if owner_id and session.scalar(select(Exclusion.id).where(
                    Exclusion.owner_id == owner_id,
                    Exclusion.normalized_key.in_(identities),
                ).limit(1)):
                    return None, "This company is excluded for this owner."
                if any(guard.token and guard.expires_at and _aware(guard.expires_at) > _aware(now) for guard in guards):
                    return None, "Another outreach attempt is currently reserved for this company or recipient."
                company_conditions = (
                    [
                        OutreachEvent.company_key == f"domain:{canonical_domain}",
                        OutreachEvent.company_key == f"name:{normalized_name}",
                    ]
                    if canonical_domain and normalized_name
                    else [OutreachEvent.company_key == f"domain:{canonical_domain}"]
                    if canonical_domain
                    else [
                        OutreachEvent.company_key == f"name:{normalized_name}",
                        OutreachEvent.normalized_company_name == normalized_name,
                    ]
                )
                sent = session.scalar(select(OutreachEvent.id).where(
                    OutreachEvent.delivery_status == "sent",
                    OutreachEvent.sent_at >= cutoff,
                    or_(OutreachEvent.normalized_email == email, *company_conditions),
                ).limit(1))
                if sent:
                    return None, "Recently contacted through this service."
                for guard in guards:
                    guard.token = token
                    guard.expires_at = now + timedelta(days=cooldown_days or config.DUPLICATE_COOLDOWN_DAYS)
                company = _company_row(session, company_name, canonical_domain)
                session.add(OutreachEvent(
                    run_id=run_id,
                    owner_id=owner_id or config.LOCAL_OWNER_ID,
                    company_id=company.id if company else None,
                    company_key=company_key(company_name, canonical_domain),
                    normalized_company_name=normalized_name,
                    recipient_email=email,
                    normalized_email=email,
                    delivery_status="reserved",
                    reservation_token=token,
                ))
            return token, None
        except Exception:
            session.rollback()
            raise


def complete_outreach(
    reservation_token: str,
    delivery_status: str,
    *,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    if delivery_status not in {"sent", "dry_run", "failed", "unknown"}:
        raise ValueError(f"Unsupported delivery status: {delivery_status}")
    with Session(get_engine()) as session, session.begin():
        event = session.scalar(select(OutreachEvent).where(
            OutreachEvent.reservation_token == reservation_token,
        ).with_for_update())
        if event is None:
            raise LookupError("Outreach reservation was not found.")
        event.delivery_status = delivery_status
        event.provider_message_id = message_id
        event.error_information = error
        if delivery_status == "sent":
            event.sent_at = utc_now()
        if delivery_status != "unknown":
            guards = session.scalars(select(ReservationGuard).where(
                ReservationGuard.token == reservation_token,
            ))
            for guard in guards:
                guard.token = None
                guard.expires_at = None


def mark_outreach_sending(reservation_token: str) -> None:
    with Session(get_engine()) as session, session.begin():
        event = session.scalar(select(OutreachEvent).where(
            OutreachEvent.reservation_token == reservation_token,
        ).with_for_update())
        if event is None:
            raise LookupError("Outreach reservation was not found.")
        event.delivery_status = "sending"


def record_provider_usage(
    *,
    run_id: str,
    provider: str,
    operation: str,
    successful: bool,
    duration_ms: Optional[int] = None,
    model: Optional[str] = None,
    retry_count: int = 0,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    estimated_cost: Optional[float] = None,
    cache_hit: Optional[bool] = None,
    error: Optional[str] = None,
) -> None:
    with Session(get_engine()) as session, session.begin():
        session.add(ProviderUsage(
            run_id=run_id,
            provider=provider,
            operation=operation,
            model=model,
            successful=successful,
            duration_ms=duration_ms,
            retry_count=retry_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            cache_hit=cache_hit,
            error_information=error,
        ))


def ensure_external_call_allowed(run_id: str) -> None:
    if not run_id or run_id == "unknown":
        return
    with Session(get_engine()) as session:
        rows = session.scalar(
            select(func.count(ProviderUsage.id)).where(
                ProviderUsage.run_id == run_id,
                ProviderUsage.provider.not_in(("cache", "mock")),
            )
        )
    if (rows or 0) >= config.MAX_EXTERNAL_CALLS_PER_RUN:
        raise RuntimeError(
            f"External call limit reached for this run ({config.MAX_EXTERNAL_CALLS_PER_RUN})."
        )


def get_usage_summary(run_id: str) -> dict[str, Any]:
    with Session(get_engine()) as session:
        rows = list(session.scalars(select(ProviderUsage).where(ProviderUsage.run_id == run_id)))
    return {
        "operation_count": len(rows),
        "successful_operations": sum(row.successful for row in rows),
        "failed_operations": sum(not row.successful for row in rows),
        "cache_hits": sum(row.cache_hit is True for row in rows),
        "cache_misses": sum(row.cache_hit is False for row in rows),
        "measured_input_tokens": sum(row.input_tokens or 0 for row in rows) if any(row.input_tokens is not None for row in rows) else None,
        "measured_output_tokens": sum(row.output_tokens or 0 for row in rows) if any(row.output_tokens is not None for row in rows) else None,
        "estimated_cost": sum(row.estimated_cost or 0 for row in rows) if any(row.estimated_cost is not None for row in rows) else None,
    }


def is_contact_verification_fresh(contact: Contact, freshness_days: Optional[int] = None) -> bool:
    if contact.verification_status not in {"valid", "invalid"} or contact.last_verified_at is None:
        return False
    age = utc_now() - _aware(contact.last_verified_at).replace(tzinfo=None)
    return age <= timedelta(days=freshness_days or config.VERIFICATION_FRESHNESS_DAYS)


def clear_engine_cache() -> None:
    """Clear cached engines; intended for tests using isolated database URLs."""
    with _engine_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _engine_for.cache_clear()
