"""Parser tuning validation and cache identity without OCR or model downloads."""

import pytest
from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from pydantic import ValidationError

from docvault import parsing, processing
from docvault.config import Settings
from docvault.models import Version


@pytest.mark.parametrize("decoder_threads", [None, 3])
def test_converter_applies_independent_inference_decoder_and_batch_settings(
    monkeypatch, decoder_threads
):
    """The native decoder gets the configured thread count even when .env is not exported."""
    settings = Settings(
        _env_file=None,
        docling_num_threads=6,
        docling_parser_threads=decoder_threads,
        docling_layout_batch_size=7,
        docling_ocr_batch_size=2,
        docling_table_batch_size=3,
    )
    monkeypatch.setattr(parsing, "get_settings", lambda: settings)
    parsing._get_document_converter.cache_clear()
    try:
        option = parsing._get_document_converter().format_to_options[InputFormat.PDF]
        assert isinstance(option.backend_options, ThreadedDoclingParseBackendOptions)
        assert option.backend_options.parser_threads == (decoder_threads or 6)
        options = option.pipeline_options
        assert isinstance(options, PdfPipelineOptions)
        assert options.accelerator_options.num_threads == 6
        assert options.layout_batch_size == 7
        assert options.ocr_batch_size == 2
        assert options.table_batch_size == 3
        assert options.do_ocr and options.do_table_structure
        assert isinstance(options.ocr_options, RapidOcrOptions)
        assert options.ocr_options.backend == "torch"
    finally:
        parsing._get_document_converter.cache_clear()


@pytest.mark.parametrize(
    "field",
    [
        "docling_num_threads",
        "docling_parser_threads",
        "docling_layout_batch_size",
        "docling_ocr_batch_size",
        "docling_table_batch_size",
    ],
)
def test_parser_concurrency_rejects_zero(field):
    """Invalid concurrency fails during configuration loading, before model initialization."""
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: 0})


def test_parser_fingerprint_changes_when_chunk_capacity_changes(monkeypatch):
    """Changing chunk capacity must invalidate otherwise identical cached parser artifacts."""
    version = Version(sha256="test-hash", filename="policy.txt", mime_type="text/plain")
    current = processing.calculate_parser_fingerprint(version)
    monkeypatch.setattr(processing, "CHUNK_TOKENS", 600)
    assert processing.calculate_parser_fingerprint(version) != current


def test_parser_tuning_loads_from_dotenv_without_exporting_variables(tmp_path, monkeypatch):
    """A reviewer can tune a worker in .env without shell-exporting Docling variables."""
    names = [
        "DOCLING_NUM_THREADS",
        "DOCLING_PARSER_THREADS",
        "DOCLING_LAYOUT_BATCH_SIZE",
        "DOCLING_OCR_BATCH_SIZE",
        "DOCLING_TABLE_BATCH_SIZE",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DOCLING_NUM_THREADS=2\nDOCLING_PARSER_THREADS=3\nDOCLING_LAYOUT_BATCH_SIZE=5\n"
        "DOCLING_OCR_BATCH_SIZE=6\nDOCLING_TABLE_BATCH_SIZE=7\n"
    )
    settings = Settings(_env_file=dotenv)
    assert settings.docling_num_threads == 2
    assert settings.docling_parser_threads == 3
    assert settings.docling_layout_batch_size == 5
    assert settings.docling_ocr_batch_size == 6
    assert settings.docling_table_batch_size == 7
