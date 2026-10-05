import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.schemas import (
    SCHEMA_VERSION,
    DocumentRecord,
    EvidenceSpan,
    EvidenceSpanError,
    MaterialityBand,
    ScenarioBand,
    SourceFamily,
    SourceRecord,
    canonical_json,
    compute_content_sha256,
    validate_evidence_span,
)

SOURCE_ID = "src-1"


def make_document(**overrides: Any) -> dict[str, Any]:
    raw_text = overrides.pop("raw_text", "Acme Corp disclosed a cyber incident.")
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "document_id": "doc-1",
        "source_id": SOURCE_ID,
        "source_url": "https://example.test/acme",
        "published_at": "2025-03-01T09:30:00-05:00",
        "observed_at": "2025-03-01T15:00:00Z",
        "retrieved_at": "2025-03-01T15:00:05+00:00",
        "raw_text": raw_text,
        "content_sha256": compute_content_sha256(raw_text),
        "is_replay": True,
        "is_synthetic": True,
    }
    data.update(overrides)
    return data


def make_span(document: DocumentRecord, start: int, end: int, quote: str) -> EvidenceSpan:
    return EvidenceSpan(
        schema_version=SCHEMA_VERSION,
        document_id=document.document_id,
        document_content_sha256=document.content_sha256,
        start_char=start,
        end_char=end,
        quote=quote,
    )


# --- documents -------------------------------------------------------------


def test_document_round_trip() -> None:
    document = DocumentRecord.model_validate(make_document())
    assert document.published_at == datetime(2025, 3, 1, 14, 30, tzinfo=UTC)
    assert document.published_at is not None and document.published_at.utcoffset() == UTC.utcoffset(
        None
    )

    text = canonical_json(document)
    assert '"published_at":"2025-03-01T14:30:00Z"' in text
    assert DocumentRecord.model_validate_json(text) == document
    assert canonical_json(DocumentRecord.model_validate_json(text)) == text


def test_document_preserves_unknown_publication_time() -> None:
    document = DocumentRecord.model_validate(make_document(published_at=None))
    assert document.published_at is None
    assert document.published_at != document.retrieved_at

    dumped = json.loads(canonical_json(document))
    assert dumped["published_at"] is None
    assert DocumentRecord.model_validate(dumped).published_at is None


def test_document_publication_time_must_be_explicit() -> None:
    data = make_document()
    del data["published_at"]
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(data)


def test_document_accepts_empty_text_with_its_hash() -> None:
    document = DocumentRecord.model_validate(make_document(raw_text=""))
    assert document.raw_text == ""
    assert document.content_sha256 == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_document_does_not_normalize_raw_text() -> None:
    raw = "  line one\r\n\tline two \u00a0 \n"
    assert DocumentRecord.model_validate(make_document(raw_text=raw)).raw_text == raw


def test_document_rejects_naive_timestamp() -> None:
    for field in ("published_at", "observed_at", "retrieved_at"):
        with pytest.raises(ValidationError):
            DocumentRecord.model_validate(make_document(**{field: "2025-03-01T15:00:00"}))
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(observed_at=datetime(2025, 3, 1, 15)))


@pytest.mark.parametrize("value", [1_740_000_000, 1.5, True])
def test_document_rejects_non_string_timestamps(value: object) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(observed_at=value))


@pytest.mark.parametrize("value", ["1740000000", "1740000000.25"])
def test_document_rejects_numeric_epoch_strings(value: str) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(observed_at=value))


@pytest.mark.parametrize(
    "value",
    [
        "2025-03-01T10:00:00-05:00",
        "2025-03-01T20:30:00+05:30",
        "2025-03-01T15:00:00Z",
    ],
)
def test_document_accepts_iso_timestamp_offsets(value: str) -> None:
    document = DocumentRecord.model_validate(make_document(observed_at=value))
    assert document.observed_at == datetime(2025, 3, 1, 15, tzinfo=UTC)
    assert '"observed_at":"2025-03-01T15:00:00Z"' in canonical_json(document)


def test_document_rejects_mismatched_hash() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        DocumentRecord.model_validate(make_document(content_sha256="0" * 64))


@pytest.mark.parametrize(
    "bad_hash",
    ["abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65, ("0" * 64) + "\n"],
)
def test_document_rejects_malformed_hash(bad_hash: str) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(content_sha256=bad_hash))


def test_hash_covers_exact_utf8_bytes() -> None:
    assert compute_content_sha256("A🚢B") != compute_content_sha256("A🚢B ")
    document = DocumentRecord.model_validate(make_document(raw_text="A🚢B"))
    assert document.content_sha256 == compute_content_sha256("A🚢B")


@pytest.mark.parametrize("field", ["is_replay", "is_synthetic"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_document_flags_must_be_real_booleans(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(**{field: value}))


@pytest.mark.parametrize("url", ["ftp://x.test/a", "example.test/a", "https://", "http://a b/c"])
def test_document_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(source_url=url))


@pytest.mark.parametrize(
    "url", ["https://example.test:not-a-port/a", "https://example.test:99999/a"]
)
def test_document_rejects_invalid_source_url_ports(url: str) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(source_url=url))


def test_document_source_url_is_optional_and_kept_verbatim() -> None:
    assert DocumentRecord.model_validate(make_document(source_url=None)).source_url is None
    url = "HTTPS://Example.test/A?q=1#frag"
    assert DocumentRecord.model_validate(make_document(source_url=url)).source_url == url


def test_replay_and_synthetic_are_independent() -> None:
    real_replay = DocumentRecord.model_validate(make_document(is_synthetic=False))
    assert real_replay.is_replay and not real_replay.is_synthetic


def test_records_are_immutable() -> None:
    document = DocumentRecord.model_validate(make_document())
    with pytest.raises(ValidationError):
        document.raw_text = "changed"  # type: ignore[misc]


def test_document_requires_schema_version() -> None:
    data = make_document()
    del data["schema_version"]
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(data)
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(schema_version="trace-core-v9"))


@pytest.mark.parametrize("bad_id", ["", "   ", 7, None])
def test_ids_must_be_nonblank_strings(bad_id: object) -> None:
    with pytest.raises(ValidationError):
        DocumentRecord.model_validate(make_document(document_id=bad_id))


# --- sources ---------------------------------------------------------------


def make_source(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "source_family": "NEWS_WIRE",
    }
    data.update(overrides)
    return data


def test_source_unknowns_stay_null() -> None:
    source = SourceRecord.model_validate(make_source())
    assert source.independence_group_id is None
    assert source.origin_id is None
    assert source.quoted_source_id is None
    assert source.source_tier is None
    assert json.loads(canonical_json(source))["independence_group_id"] is None


def test_source_round_trip_with_supplied_classifications() -> None:
    source = SourceRecord.model_validate(
        make_source(
            source_family=SourceFamily.SEC_REGULATORY,
            origin_id="origin-1",
            quoted_source_id="src-0",
            independence_group_id="grp-1",
            source_tier=1,
        )
    )
    assert SourceRecord.model_validate_json(canonical_json(source)) == source


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_family": "BLOG"},
        {"source_family": "news_wire"},
        {"source_tier": 0},
        {"source_tier": -1},
        {"source_tier": True},
        {"source_tier": 1.0},
        {"origin_id": ""},
    ],
)
def test_source_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SourceRecord.model_validate(make_source(**overrides))


# --- unknown fields --------------------------------------------------------


def test_contract_rejects_unknown_fields() -> None:
    document = DocumentRecord.model_validate(make_document())
    with pytest.raises(ValidationError, match="confidence"):
        DocumentRecord.model_validate(make_document(confidence=0.9))
    with pytest.raises(ValidationError, match="confidence"):
        SourceRecord.model_validate(make_source(confidence=0.9))
    with pytest.raises(ValidationError, match="confidence"):
        EvidenceSpan.model_validate(
            {
                **make_span(document, 0, 4, "Acme").model_dump(mode="json"),
                "confidence": 0.9,
            }
        )


# --- evidence spans --------------------------------------------------------


def test_span_matches_document() -> None:
    document = DocumentRecord.model_validate(make_document())
    span = make_span(document, 0, 9, "Acme Corp")
    assert validate_evidence_span(span, document) is span


def test_span_handles_unicode_code_points() -> None:
    document = DocumentRecord.model_validate(make_document(raw_text="A🚢B"))
    assert len(document.raw_text) == 3  # three code points (JavaScript sees four units)
    span = make_span(document, 1, 2, "🚢")
    validate_evidence_span(span, document)


def test_span_rejects_different_quote() -> None:
    document = DocumentRecord.model_validate(make_document())
    with pytest.raises(EvidenceSpanError, match="quote"):
        validate_evidence_span(make_span(document, 0, 9, "Acme Inc."), document)


def test_span_rejects_out_of_range_end() -> None:
    document = DocumentRecord.model_validate(make_document(raw_text="abc"))
    with pytest.raises(EvidenceSpanError, match="exceeds"):
        validate_evidence_span(make_span(document, 1, 10, "bc"), document)


def test_span_rejects_other_document_or_text_version() -> None:
    document = DocumentRecord.model_validate(make_document())
    other_id = DocumentRecord.model_validate(make_document(document_id="doc-2"))
    with pytest.raises(EvidenceSpanError, match="document_id"):
        validate_evidence_span(make_span(other_id, 0, 4, "Acme"), document)

    changed = DocumentRecord.model_validate(make_document(raw_text="Acme Corp disclosed."))
    with pytest.raises(EvidenceSpanError, match="sha256"):
        validate_evidence_span(make_span(changed, 0, 4, "Acme"), document.model_copy(
            update={"document_id": changed.document_id}
        ))


def test_empty_document_cannot_support_a_span() -> None:
    document = DocumentRecord.model_validate(make_document(raw_text=""))
    with pytest.raises(ValidationError):
        make_span(document, 0, 0, "")
    with pytest.raises(EvidenceSpanError):
        validate_evidence_span(make_span(document, 0, 1, "x"), document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: {**d, "start_char": -1},
        lambda d: {**d, "end_char": d["start_char"]},
        lambda d: {**d, "end_char": 0, "start_char": 0},
        lambda d: {**d, "start_char": True},
        lambda d: {**d, "end_char": 5.0},
        lambda d: {**d, "start_char": "0"},
        lambda d: {**d, "quote": ""},
        lambda d: {**d, "document_content_sha256": "xyz"},
    ],
)
def test_span_shape_validation(mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    base = {
        "schema_version": SCHEMA_VERSION,
        "document_id": "doc-1",
        "document_content_sha256": "0" * 64,
        "start_char": 0,
        "end_char": 4,
        "quote": "Acme",
    }
    EvidenceSpan.model_validate(base)
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(mutate(base))


# --- enums -----------------------------------------------------------------


def test_materiality_and_scenario_bands_are_distinct() -> None:
    assert [band.value for band in MaterialityBand] == ["low", "medium", "high"]
    assert [band.value for band in ScenarioBand] == ["low", "central", "adverse"]
    assert MaterialityBand is not ScenarioBand


def test_serialization_is_deterministic() -> None:
    first = canonical_json(DocumentRecord.model_validate(make_document()))
    second = canonical_json(DocumentRecord.model_validate(make_document()))
    assert first == second
    assert list(json.loads(first)) == sorted(json.loads(first))


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "infinity", "-infinity"]
)
def test_canonical_json_rejects_nonfinite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})
