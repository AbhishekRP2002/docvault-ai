"""Server-scoped document tools with explicit provenance and current operational state."""

import asyncio
from collections import Counter

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from docvault.cache import calculate_json_fingerprint
from docvault.db import session
from docvault.documents import require_document, require_version, require_versions
from docvault.errors import AppError
from docvault.llm.evidence import build_llm_evidence_record
from docvault.llm.models import Evidence
from docvault.llm.provider import OpenRouterLLM
from docvault.models import Artifact, Chunk, Document, Job, Version, now
from docvault.retrieval import create_cited_evidence_record, retrieve_relevant_chunks
from docvault.tools.discovery import search_document_overviews
from docvault.tools.models import TOOL_INPUT_MODELS, ToolExecutionResult

OVERVIEW_DOCUMENT_PAGE_SIZE = 5
SUPPORTING_SNIPPET_LIMIT = 3
SUMMARY_PREVIEW_CHARACTERS = 1600
INSIGHT_PREVIEW_CHARACTERS = 400


def build_analysis_preview(data: dict, allowed_ids: set[str]) -> dict:
    """Expose bounded derived findings with only the preview's validated source references."""
    preview: dict = {}
    if data.get("summary"):
        preview["summary"] = str(data["summary"])[:SUMMARY_PREVIEW_CHARACTERS]
        preview["summary_note"] = (
            "Derived orientation only; substantiate factual claims with the supplied original passages. This preview does not establish complete source coverage."
        )
    preview["key_insights"] = [
        {
            "text": str(insight.get("text", ""))[:INSIGHT_PREVIEW_CHARACTERS],
            "citation_ids": [
                source for source in insight.get("citation_ids", []) if source in allowed_ids
            ],
        }
        for insight in data.get("key_insights", [])[:4]
        if insight.get("citation_ids") and set(insight["citation_ids"]).issubset(allowed_ids)
    ]
    findings = []
    for row in data.get("rows", []):
        for cell in row.get("cells", []):
            if len(findings) == 3:
                break
            identifiers = [
                source for source in cell.get("citation_ids", []) if source in allowed_ids
            ]
            if cell.get("status") == "found" and (
                not identifiers or not set(cell.get("citation_ids", [])).issubset(allowed_ids)
            ):
                continue
            findings.append(
                {
                    "dimension": str(row.get("dimension", ""))[:80],
                    "version_id": cell.get("version_id"),
                    "status": cell.get("status"),
                    "text": str(cell.get("text", ""))[:INSIGHT_PREVIEW_CHARACTERS],
                    "citation_ids": identifiers,
                }
            )
        if len(findings) == 3:
            break
    if findings:
        preview["findings"] = findings
    preview["citation_ids"] = [
        identifier
        for identifier in collect_analysis_citation_ids(data)
        if identifier in allowed_ids
    ]
    preview["preview_only"] = True
    return preview


def select_analysis_supporting_ids(data: dict) -> list[str]:
    """Prefer original sources of individually cited insights over broad summary references."""
    groups = [insight.get("citation_ids", []) for insight in data.get("key_insights", [])]
    groups.extend(
        cell.get("citation_ids", [])
        for row in data.get("rows", [])
        for cell in row.get("cells", [])
        if cell.get("status") == "found"
    )
    selected: list[str] = []
    for group in groups:
        addition = [identifier for identifier in dict.fromkeys(group) if identifier not in selected]
        if len(selected) + len(addition) <= SUPPORTING_SNIPPET_LIMIT:
            selected.extend(addition)
    for identifier in collect_analysis_citation_ids(data):
        if identifier not in selected and len(selected) < SUPPORTING_SNIPPET_LIMIT:
            selected.append(identifier)
    return selected


def build_supporting_snippet(evidence: Evidence) -> dict:
    """Keep a bounded original passage excerpt; full source text remains in public citations."""
    snippet = build_llm_evidence_record(evidence)
    snippet["text"] = evidence.text[:1200]
    snippet["excerpt_truncated"] = len(evidence.text) > 1200
    return snippet


def resolve_original_citation_evidence(
    db: Session, citation_ids: list[str], version_ids: list[str]
) -> list[Evidence]:
    """Resolve only original passages inside the captured source scope; reject forged references."""
    identifiers = list(dict.fromkeys(citation_ids))
    chunks = list(
        db.scalars(
            select(Chunk).where(Chunk.id.in_(identifiers), Chunk.version_id.in_(version_ids))
        )
    )
    by_id = {chunk.id: chunk for chunk in chunks}
    if set(identifiers) != set(by_id):
        raise AppError(
            409, "invalid_source_references", "Stored analysis has unavailable source references."
        )
    versions = {
        identifier: require_version(db, identifier, ready=True) for identifier in version_ids
    }
    return [
        create_cited_evidence_record(by_id[identifier], versions[by_id[identifier].version_id])
        for identifier in identifiers
    ]


def collect_analysis_citation_ids(data: dict) -> list[str]:
    """Collect original references from a summary's insights or a comparison's result cells."""
    identifiers = list(data.get("citation_ids", []))
    identifiers.extend(
        source
        for insight in data.get("key_insights", [])
        for source in insight.get("citation_ids", [])
    )
    identifiers.extend(
        source
        for row in data.get("rows", [])
        for cell in row.get("cells", [])
        for source in cell.get("citation_ids", [])
    )
    return list(dict.fromkeys(identifiers))


class DocumentToolRuntime:
    """Bind every tool to a turn's immutable selected versions and accounting resource."""

    def __init__(self, version_ids: list[str], message_id: str, llm: OpenRouterLLM):
        """Capture backend-issued scope once; model arguments cannot expand selected sources."""
        self.version_ids = list(dict.fromkeys(version_ids))
        self.message_id = message_id
        self.llm = llm

    def metadata(self) -> list[dict]:
        """Return selected identities and availability without injecting derived document content."""
        with session() as db:
            items = []
            for identifier in self.version_ids:
                version = require_version(db, identifier)
                document = require_document(db, version.document_id)
                insights = version.insights or {}
                items.append(
                    {
                        "version_id": version.id,
                        "document_id": document.id,
                        "filename": version.filename,
                        "title": document.title,
                        "version_number": version.version_number,
                        "status": version.status,
                        "sha256": version.sha256,
                        "insight_status": version.insight_status,
                        "current_version_id": document.current_version_id,
                        "latest_version_id": document.latest_version_id,
                        "overview_generation_fingerprint": insights.get("generation", {}).get(
                            "fingerprint"
                        )
                        or calculate_json_fingerprint(insights),
                    }
                )
            return items

    async def read_previous_citation_evidence(
        self, citation_ids: list[str], version_ids: list[str]
    ) -> list[Evidence]:
        """Reload original prior-answer sources only when the captured source snapshot matches.

        Formatting follow-ups may reuse validated source IDs, never prior model text as
        evidence. Changed selections discard reuse; forged or deleted references fail safely.
        """
        if set(version_ids) != set(self.version_ids) or not version_ids or not citation_ids:
            return []

        def load_previous_sources() -> list[Evidence]:
            """Resolve original source rows with live/ready guards inside an owned DB session."""
            with session() as db:
                require_versions(db, self.version_ids)
                return resolve_original_citation_evidence(db, citation_ids, self.version_ids)

        return await asyncio.to_thread(load_previous_sources)

    def _check_selected_version(self, identifier: str) -> None:
        """Reject a document reference outside this turn's captured source versions."""
        if identifier not in self.version_ids:
            raise AppError(
                403,
                "source_out_of_scope",
                "Choose this document as a chat source before reading it.",
            )

    async def execute(self, name: str, arguments: dict) -> ToolExecutionResult:
        """Validate native arguments, execute a scoped handler, and return safe application errors."""
        try:
            model = TOOL_INPUT_MODELS.get(name)
            if model is None:
                raise AppError(422, "unknown_tool", "This tool is unavailable.")
            args = model.model_validate(arguments, strict=True).model_dump()
            if (
                name not in {"search_documents", "get_document_processing_metrics"}
                and not self.version_ids
            ):
                return ToolExecutionResult(
                    content={
                        "status": "selection_needed",
                        "message": "Select documents to use this tool.",
                    },
                    scope="operational",
                )
            if name == "retrieve_relevant_chunks":
                query = args["query"].strip()
                if not query:
                    raise AppError(
                        422, "invalid_tool_arguments", "Provide a nonempty search question."
                    )
                evidence = await retrieve_relevant_chunks(query, self.version_ids, self.llm)
                return ToolExecutionResult(
                    content={
                        "status": "ok",
                        "passages": [build_llm_evidence_record(item) for item in evidence],
                        "match_count": len(evidence),
                        "coverage": "Hybrid semantic and keyword matches; not a whole-document read.",
                        **(
                            {
                                "next_step": "Call get_selected_document_overviews before concluding evidence is unavailable; it provides bounded original supporting snippets.",
                            }
                            if not evidence
                            else {}
                        ),
                    },
                    evidence=evidence,
                    scope="document",
                    query=query,
                )
            if name == "search_documents":
                query = args["query"].strip()
                if not query:
                    raise AppError(422, "invalid_tool_arguments", "Provide a nonempty topic.")
                content = await search_document_overviews(query, self.llm, args["cursor"])
                return ToolExecutionResult(content=content, scope="workspace", query=query)
            handlers = {
                "get_selected_document_overviews": self._get_selected_document_overviews,
                "get_document_processing_metrics": self._get_document_processing_metrics,
                "get_document_processing_diagnostics": self._get_document_processing_diagnostics,
                "get_document_analysis_result": self._get_document_analysis_result,
            }
            return await asyncio.to_thread(handlers[name], **args)
        except ValidationError:
            return ToolExecutionResult(
                content={
                    "status": "error",
                    "error": {
                        "code": "invalid_tool_arguments",
                        "message": "Tool arguments do not match the required schema.",
                        "retryable": False,
                    },
                },
                scope="operational",
            )
        except AppError as exc:
            return ToolExecutionResult(
                content={
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                },
                scope="operational",
            )

    def _get_selected_document_overviews(self, cursor: int | None) -> ToolExecutionResult:
        """Read bounded derived previews or a labeled source sample, never traverse a raw file."""
        offset, consumed, items, evidence = cursor or 0, 0, [], []
        with session() as db:
            for identifier in self.version_ids[offset : offset + OVERVIEW_DOCUMENT_PAGE_SIZE]:
                version = require_version(db, identifier)
                insights = version.insights or {}
                item: dict = {
                    "version_id": version.id,
                    "document_id": version.document_id,
                    "filename": version.filename,
                    "status": version.insight_status,
                    "source_status": version.status,
                }
                original: list[Evidence] = []
                if version.insight_status == "ready" and insights:
                    citation_ids = select_analysis_supporting_ids(insights)
                    if not citation_ids:
                        raise AppError(
                            409,
                            "invalid_source_references",
                            "Stored overview has no original supporting references.",
                        )
                    original = resolve_original_citation_evidence(db, citation_ids, [identifier])
                    item.update(build_analysis_preview(insights, set(citation_ids)))
                    item.update(
                        category=insights.get("category"),
                        tags=insights.get("tags", [])[:8],
                        coverage={
                            "complete": False,
                            "kind": "derived_overview_preview",
                            "source_analysis_coverage": insights.get("coverage"),
                        },
                    )
                elif version.status == "ready":
                    total = (
                        db.scalar(
                            select(func.count())
                            .select_from(Chunk)
                            .where(Chunk.version_id == identifier)
                        )
                        or 0
                    )
                    ordinals = {0, total // 2, max(0, total - 1)}
                    chunks = db.scalars(
                        select(Chunk)
                        .where(Chunk.version_id == identifier, Chunk.ordinal.in_(ordinals))
                        .order_by(Chunk.ordinal)
                    )
                    original = [create_cited_evidence_record(chunk, version) for chunk in chunks]
                    item.update(
                        message="Generated overview is unavailable. These first/middle/last source samples support only a limited overview; use the dashboard summary feature for complete analysis.",
                        coverage={
                            "complete": False,
                            "kind": "sampled_source_passages",
                            "sampled_chunks": len(original),
                            "total_chunks": total,
                        },
                    )
                else:
                    item.update(
                        message="Source processing is not ready; no document content is available.",
                        coverage={"complete": False},
                    )
                item["supporting_snippets"] = [
                    build_supporting_snippet(record) for record in original
                ]
                items.append(item)
                evidence.extend(original)
                consumed += 1
        end = offset + consumed
        return ToolExecutionResult(
            content={
                "status": "ok",
                "items": items,
                "next_cursor": end if end < len(self.version_ids) else None,
                "coverage": {
                    "documents_returned": consumed,
                    "selected_documents": len(self.version_ids),
                    "complete": offset == 0 and end == len(self.version_ids),
                    "content_preview_only": True,
                },
            },
            evidence=evidence,
            scope="document",
        )

    def _get_document_processing_metrics(self, scope: str) -> ToolExecutionResult:
        """Count latest uploaded revisions once per live document, keeping insight states separate."""
        with session() as db:
            filters = [Document.deleted_at.is_(None)]
            if scope == "selected":
                if not self.version_ids:
                    return ToolExecutionResult(
                        content={
                            "status": "selection_needed",
                            "message": "Select documents to view their status.",
                        },
                        scope="operational",
                    )
                selected = require_versions(db, self.version_ids, ready=False)
                filters.append(Document.id.in_({version.document_id for version in selected}))
            current = aliased(Version)
            rows = list(
                db.execute(
                    select(
                        Version.status,
                        Version.insight_status,
                        current.status.label("current_status"),
                    )
                    .select_from(Document)
                    .outerjoin(Version, Version.id == Document.latest_version_id)
                    .outerjoin(current, current.id == Document.current_version_id)
                    .where(*filters)
                )
            )
            states = Counter(row.status for row in rows)
            processing = sum(
                states[state] for state in ("parsing", "chunking", "embedding", "processing")
            )
            counts = {
                "total": len(rows),
                "queued": states["queued"],
                "processing": processing,
                "ready": states["ready"],
                "failed": states["failed"],
                "other": len(rows)
                - states["queued"]
                - processing
                - states["ready"]
                - states["failed"],
            }
            return ToolExecutionResult(
                content={
                    "status": "ok",
                    "scope": scope,
                    "observed_at": now().isoformat(),
                    "counts": counts,
                    "insights": dict(Counter(row.insight_status or "unavailable" for row in rows)),
                    "documents_with_ready_source": sum(
                        row.current_status == "ready" for row in rows
                    ),
                },
                scope="operational",
            )

    def _get_document_processing_diagnostics(self, version_id: str) -> ToolExecutionResult:
        """Read safe persisted attempts/stages for selected sources without implicitly replaying jobs."""
        from docvault.api.diagnostics import serialize_job_diagnostics

        self._check_selected_version(version_id)
        with session() as db:
            require_version(db, version_id)
            jobs = list(
                db.scalars(
                    select(Job)
                    .where(
                        Job.resource_id == version_id,
                        Job.kind.in_(["ingest", "insights", "overview_index"]),
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(20)
                )
            )
            return ToolExecutionResult(
                content={
                    "status": "ok",
                    "observed_at": now().isoformat(),
                    "items": [
                        item.model_dump(mode="json") for item in serialize_job_diagnostics(db, jobs)
                    ],
                },
                scope="operational",
            )

    def _get_document_analysis_result(self, artifact_id: str) -> ToolExecutionResult:
        """Read saved status and a bounded analysis preview only within captured source scope."""
        with session() as db:
            artifact = db.get(Artifact, artifact_id)
            if artifact is None:
                raise AppError(404, "artifact_not_found", "Result not found.")
            if not artifact.version_ids or set(artifact.version_ids) - set(self.version_ids):
                raise AppError(
                    403,
                    "source_out_of_scope",
                    "Choose all result sources before reading this analysis.",
                )
            require_versions(db, artifact.version_ids)
            content: dict = {
                "status": "ok",
                "artifact_id": artifact.id,
                "kind": artifact.kind,
                "artifact_status": artifact.status,
                "error": artifact.error,
                "observed_at": now().isoformat(),
                "result_path": f"/v1/artifacts/{artifact.id}",
            }
            evidence = []
            if artifact.status == "ready" and artifact.data:
                identifiers = select_analysis_supporting_ids(artifact.data)
                evidence = resolve_original_citation_evidence(db, identifiers, artifact.version_ids)
                content["preview"] = build_analysis_preview(artifact.data, set(identifiers))
                content["supporting_snippets"] = [
                    build_supporting_snippet(item) for item in evidence
                ]
            return ToolExecutionResult(
                content=content, evidence=evidence, scope="document" if evidence else "operational"
            )
