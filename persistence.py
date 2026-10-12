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
    DateTime,
    ForeignKey,
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
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
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
