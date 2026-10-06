"""Compact model-facing provenance while retaining complete stored citation geometry."""

from copy import deepcopy

from docvault.llm.models import Evidence

LLM_LOCATION_FIELDS = frozenset(
    {
        "kind",
        "label",
        "page",
        "pages",
        "heading",
        "headings",
        "section",
        "paragraph",
        "line_start",
        "line_end",
        "char_start",
        "char_end",
    }
)


def build_llm_evidence_record(evidence: Evidence) -> dict:
    """Keep original IDs, full passage text and readable locators; omit rendering geometry.

    Bounding boxes and repeated item/source spans belong to citation rendering and remain
    in the original Evidence/database record. They convey no additional passage facts and
    can dwarf the document text in model context. This does not truncate extracted text.
    """
    record = evidence.model_dump(exclude={"location"})
    record["location"] = deepcopy(
        {key: value for key, value in evidence.location.items() if key in LLM_LOCATION_FIELDS}
    )
    return record
