"""Initial TRACE data contracts: `SourceRecord`, `DocumentRecord`, `EvidenceSpan`.

Conventions:

* Domain records are frozen and reject unknown fields.
* IDs are opaque, nonblank, caller-supplied strings. Nothing here generates
  IDs or timestamps.
* Unknown information is an explicit ``null``; it is never defaulted to a
  "safe" value (no zero, no "independent", no retrieval-time publication time).
* Timestamps must be timezone-aware and are normalized to UTC (same instant).
* Counts and offsets are real integers; booleans are real booleans.
* Validation checks *shape and internal consistency* only. It does not verify
  that a claim is true, and it never touches a database, network or model.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.domain.schemas.enums import SourceFamily

SCHEMA_VERSION = "trace-core-v0.1"


# --------------------------------------------------------------------------- #
# Field types
# --------------------------------------------------------------------------- #


def _require_timestamp_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp string or datetime") from exc
    raise ValueError("must be an ISO-8601 timestamp string or datetime")


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _check_http_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"is not a valid URL: {exc}") from exc
    if parts.scheme not in {"http", "https"} or not hostname:
        raise ValueError("must be an absolute http(s) URL with a host")
    return value


NonBlankId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, pattern=r"\S"),
    Field(description="Opaque, nonblank, caller-supplied identifier."),
]
UtcTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_require_timestamp_input),
    AfterValidator(_to_utc),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
    Field(description="SHA-256, lowercase hexadecimal, 64 characters."),
]
HttpUrl = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[Hh][Tt][Tt][Pp][Ss]?://[^\s/?#]+[^\s]*$"),
    AfterValidator(_check_http_url),
]


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["trace-core-v0.1"]


# --------------------------------------------------------------------------- #
# Hashing and serialization
# --------------------------------------------------------------------------- #


def compute_content_sha256(raw_text: str) -> str:
    """SHA-256 of the UTF-8 encoding of ``raw_text``. No cleanup is applied."""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def canonical_json(value: BaseModel | Any, *, indent: int | None = None) -> str:
    """Deterministic JSON: sorted keys, UTC timestamps, array order preserved."""
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


class SourceRecord(_ContractModel):
    """A source/provenance identity with *supplied* classifications.

    This model stores classifications; it does not decide them. ``None`` means
    unknown. In particular, ``independence_group_id=None`` is not
    "independent", and independence is never derived from ``source_family``.
    """

    source_id: NonBlankId
    source_family: SourceFamily
    origin_id: NonBlankId | None = None
    quoted_source_id: NonBlankId | None = None
    independence_group_id: NonBlankId | None = None
    source_tier: Annotated[int, Field(strict=True, gt=0)] | None = None


class DocumentRecord(_ContractModel):
    """Immutable raw document text with provenance.

    ``raw_text`` is retained exactly as supplied (never trimmed or normalized);
    normalization derives a separate representation later.
    """

    document_id: NonBlankId
    source_id: NonBlankId
    source_url: HttpUrl | None = None
    published_at: UtcTimestamp | None = Field(
        description="Explicit publication time, or null when unknown. "
        "Never substituted with observed_at/retrieved_at."
    )
    observed_at: UtcTimestamp
    retrieved_at: UtcTimestamp
    raw_text: str = Field(
        strict=True, description="Exact decoded text retained as evidence; may be empty."
    )
    content_sha256: Sha256Hex = Field(description="SHA-256 of the UTF-8 encoding of raw_text.")
    is_replay: bool = Field(strict=True)
    is_synthetic: bool = Field(strict=True)

    @model_validator(mode="after")
    def _verify_content_hash(self) -> "DocumentRecord":
        try:
            expected = compute_content_sha256(self.raw_text)
        except UnicodeEncodeError as exc:
            raise ValueError("raw_text is not encodable as UTF-8") from exc
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match SHA-256 of UTF-8 raw_text")
        return self


class EvidenceSpan(_ContractModel):
    """A quote located in an immutable document text version.

    Offsets are zero-based, end-exclusive Unicode code-point offsets into
    ``raw_text`` (Python ``str`` indexing). JavaScript counts UTF-16 code units,
    so clients should display ``quote`` rather than slicing with these offsets.
    """

    document_id: NonBlankId
    document_content_sha256: Sha256Hex = Field(
        description="Identifies the text version these offsets refer to."
    )
    start_char: int = Field(strict=True, ge=0)
    end_char: int = Field(strict=True, gt=0)
    quote: str = Field(strict=True, min_length=1, description="Nonempty exact text.")

    @model_validator(mode="after")
    def _verify_offsets(self) -> "EvidenceSpan":
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        return self


# --------------------------------------------------------------------------- #
# Span/document consistency (pure; no lookups)
# --------------------------------------------------------------------------- #


class EvidenceSpanError(ValueError):
    """The span does not point at the supplied document text."""


def validate_evidence_span(span: EvidenceSpan, document: DocumentRecord) -> EvidenceSpan:
    """Check that ``span`` selects exactly ``span.quote`` from ``document``.

    Returns ``span`` on success; raises `EvidenceSpanError` otherwise.
    """
    if span.document_id != document.document_id:
        raise EvidenceSpanError(
            f"span document_id {span.document_id!r} != document {document.document_id!r}"
        )
    if span.document_content_sha256 != document.content_sha256:
        raise EvidenceSpanError("span document_content_sha256 does not match the document")
    if span.end_char > len(document.raw_text):
        raise EvidenceSpanError(
            f"end_char {span.end_char} exceeds raw_text length {len(document.raw_text)}"
        )
    if document.raw_text[span.start_char : span.end_char] != span.quote:
        raise EvidenceSpanError("quote does not equal raw_text[start_char:end_char]")
    return span
