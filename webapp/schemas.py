from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class BatchItemInput(BaseModel):
    company_name: str | None = Field(default=None, max_length=300)
    domain: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=300)
    job_description: str | None = Field(default=None, max_length=12000)

    @model_validator(mode="after")
    def require_target(self) -> "BatchItemInput":
        if not self.company_name and not self.domain:
            raise ValueError("Each batch item needs a company name or domain.")
        return self


class BatchCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=240)
    items: list[BatchItemInput] = Field(min_length=1, max_length=100)
    cv_artifact_id: str = Field(min_length=1, max_length=36)
    candidate_name: str | None = Field(default=None, max_length=200)
    example_letter_artifact_id: str | None = Field(default=None, max_length=36)


class BatchItemResponse(BaseModel):
    id: str
    status: str
    company_name: str | None = None
    domain: str | None = None
    role: str | None = None
    research_decision: str | None = None
    failure_code: str | None = None


class BatchResponse(BaseModel):
    id: str
    label: str | None
    status: str
    created_at: datetime
    item_count: int
    completed_count: int
    failed_count: int
    items: list[BatchItemResponse] = []


class BatchListResponse(BaseModel):
    items: list[BatchResponse]
    limit: int
    offset: int


class ResearchCard(BaseModel):
    item_id: str
    status: str
    decision: str | None
    company_name: str | None
    company_domain: str | None
    contact_name: str | None
    contact_email: str | None
    role: str | None
    role_fit: str | None
    summary: str | None
    failure_code: str | None


class ResearchDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class DraftGenerationRequest(BaseModel):
    regenerate_item_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_items(self) -> "DraftGenerationRequest":
        if len(self.regenerate_item_ids) != len(set(self.regenerate_item_ids)):
            raise ValueError("Regeneration item IDs must be unique.")
        return self


class DraftDocument(BaseModel):
    artifact_type: Literal["cover_letter", "email"]
    version: int
    content: str
    subject: str | None = None
    approval: str | None = None


class DraftResponse(BaseModel):
    item_id: str
    documents: list[DraftDocument]


class DraftEditRequest(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    subject: str | None = Field(default=None, max_length=500)
    expected_current_version: int = Field(ge=1)


class ApprovalRequest(BaseModel):
    artifact_type: Literal["cover_letter", "email"]
    draft_version: int = Field(ge=1)
    decision: Literal["approved", "rejected"]


class CombinedApprovalRequest(BaseModel):
    cover_letter_version: int = Field(ge=1)
    email_version: int = Field(ge=1)


class ApprovalResponse(BaseModel):
    item_id: str
    artifact_type: str
    draft_version: int
    decision: str
    decided_at: datetime


class RunEventResponse(BaseModel):
    id: str
    sequence: int
    event_type: str
    phase: str | None
    status: str | None
    progress: int | None
    item_id: str | None
    detail: str | None
    created_at: datetime


class ArtifactResponse(BaseModel):
    id: str
    artifact_type: str
    filename: str | None
    media_type: str | None
    byte_size: int | None
    created_at: datetime


class HealthResponse(BaseModel):
    status: Literal["ok", "not_ready"]
    database: Literal["available", "unavailable"]


class RunStatusResponse(BaseModel):
    id: str
    batch_id: str
    item_id: str
    status: str
    phase: str | None
    progress: int
    updated_at: datetime
