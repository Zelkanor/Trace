"""Shared TRACE contracts: enums and the initial source/document/span records."""

from app.domain.schemas.enums import (
    AssetClass,
    EvidenceStrength,
    LifecycleStatus,
    MaterialityBand,
    PolicyRoute,
    RunMode,
    ScenarioBand,
    SourceFamily,
)
from app.domain.schemas.records import (
    SCHEMA_VERSION,
    DocumentRecord,
    EvidenceSpan,
    EvidenceSpanError,
    SourceRecord,
    canonical_json,
    compute_content_sha256,
    validate_evidence_span,
)

__all__ = [
    "SCHEMA_VERSION",
    "AssetClass",
    "DocumentRecord",
    "EvidenceSpan",
    "EvidenceSpanError",
    "EvidenceStrength",
    "LifecycleStatus",
    "MaterialityBand",
    "PolicyRoute",
    "RunMode",
    "ScenarioBand",
    "SourceFamily",
    "SourceRecord",
    "canonical_json",
    "compute_content_sha256",
    "validate_evidence_span",
]
