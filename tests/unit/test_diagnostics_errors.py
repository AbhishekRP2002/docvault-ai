"""Safe error classification and isolated parser stage instrumentation."""

from contextlib import contextmanager

import pytest

from docvault.diagnostics import build_processing_error, classify_processing_failure
from docvault.errors import AppError
from docvault.jobs import SourceDeleted
from docvault.llm.provider import ContextLimitError, ProviderError
from docvault.parsing import parse_document_file
from docvault.stage_tracking import collect_processing_stages, processing_stage


@pytest.mark.parametrize(
    ("exc", "stage", "code", "retryable"),
    [
        (SourceDeleted("private"), "embedding", "source_deleted", False),
        (PermissionError("private path"), "persisting", "storage_unavailable", False),
        (FileNotFoundError("private path"), "conversion", "source_file_missing", False),
        (ImportError("private"), "conversion", "parser_dependency_missing", False),
        (ValueError("secret content"), "conversion", "conversion_failed", False),
        (ValueError("secret content"), "chunking", "chunking_failed", False),
        (RuntimeError("secret content"), "generating", "processing_failed", False),
        (TimeoutError("private"), "embedding", "dependency_unavailable", True),
        (ContextLimitError("private"), "generating", "context_limit", False),
        (
            ProviderError("Provider unavailable.", retryable=True),
            "embedding",
            "provider_unavailable",
            True,
        ),
    ],
)
def test_failure_classification_does_not_echo_raw_exception_payloads(exc, stage, code, retryable):
    """Use stable safe errors rather than exposing confidential exception text."""
    error = classify_processing_failure(exc, stage)
    assert error.code == code and error.retryable is retryable
    assert "private" not in error.message and "secret" not in error.message
    assert error.remediation


def test_known_application_errors_keep_actionable_messages():
    """Retain explicitly caller-safe messages from trusted application boundaries."""
    error = classify_processing_failure(
        AppError(409, "configuration_changed", "Submit again."), "generating"
    )
    assert error.code == "configuration_changed" and error.message == "Submit again."
    assert build_processing_error(None, None, None) is None
    legacy = build_processing_error(None, "Historical failure.", None)
    assert legacy is not None and legacy.code == "legacy_failure" and legacy.retryable is None


def test_parser_records_conversion_and_chunking_without_requiring_a_job(tmp_path):
    """Observe actual parser boundaries while retaining standalone parsing behavior."""
    stages = []

    @contextmanager
    def record(name):
        """Capture stage start/end for the pure instrumentation boundary."""
        stages.append((name, "start"))
        yield
        stages.append((name, "end"))

    source = tmp_path / "source.txt"
    source.write_text("A complete sentence.")
    with collect_processing_stages(record):
        assert parse_document_file(source, "text/plain", source.name).chunks
    with processing_stage("outside"):
        pass
    assert stages == [
        ("conversion", "start"),
        ("conversion", "end"),
        ("chunking", "start"),
        ("chunking", "end"),
    ]
