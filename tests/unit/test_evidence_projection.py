"""Original evidence stays complete while models receive useful source locators."""

from docvault.llm.evidence import build_llm_evidence_record
from docvault.llm.models import Evidence


def test_model_projection_preserves_full_passage_and_original_rendering_provenance():
    """Repeated geometry cannot consume context or mutate stored/public citation data."""
    source = Evidence(
        id="original-chunk",
        document_id="document",
        version_id="version",
        filename="guide.pdf",
        version_number=1,
        text="Complete original text. " * 2000,
        location={
            "kind": "pdf",
            "page": 4,
            "pages": [4, 5],
            "headings": ["Language models"],
            "source_spans": [{"bbox": [1, 2, 3, 4], "page": 4}] * 5000,
            "spans": [{"start": 0, "end": 10}] * 5000,
        },
    )
    before = source.model_dump()
    record = build_llm_evidence_record(source)
    assert record["id"] == source.id and record["text"] == source.text
    assert record["location"] == {
        "kind": "pdf",
        "page": 4,
        "pages": [4, 5],
        "headings": ["Language models"],
    }
    record["location"]["pages"].append(6)
    assert source.model_dump() == before
