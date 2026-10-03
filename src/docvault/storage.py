import hashlib
import os
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from docvault.config import get_settings
from docvault.errors import AppError

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


def storage_file(key: str) -> Path:
    """Resolve a storage key within the configured root and reject paths escaping it."""
    root = get_settings().storage_path.resolve()
    target = (root / key).resolve()
    if not target.is_relative_to(root):
        raise AppError(400, "invalid_path", "Invalid storage path.")
    return target


async def store_upload(upload: UploadFile) -> dict:
    """Stream, hash, and validate an upload before adopting its private storage file.

    Enforce byte and format safeguards and remove partial files on failure.
    """
    filename = Path((upload.filename or "document").replace("\\", "/")).name
    suffix = Path(filename).suffix.lower()
    if suffix not in MIME_TYPES:
        raise AppError(415, "unsupported_format", "Choose a PDF, DOCX, or UTF-8 text file.")
    key = f"sources/{uuid4()}{suffix}"
    target = storage_file(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(suffix + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with temporary.open("wb") as stream:
            while data := await upload.read(1024 * 1024):
                size += len(data)
                if size > get_settings().max_upload_bytes:
                    raise AppError(
                        413, "file_too_large", "File exceeds the configured upload size."
                    )
                digest.update(data)
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not size:
            raise AppError(422, "empty_file", "The uploaded file is empty.")
        validate_file(temporary, suffix)
        temporary.replace(target)
        return dict(
            filename=filename,
            mime_type=MIME_TYPES[suffix],
            storage_key=key,
            sha256=digest.hexdigest(),
            size_bytes=size,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise


def validate_file(path: Path, suffix: str):
    """Check PDF headers, UTF-8 text, or DOCX container structure and expansion safeguards."""
    with path.open("rb") as stream:
        prefix = stream.read(1024)
    if suffix == ".pdf" and b"%PDF-" not in prefix:
        raise AppError(415, "invalid_pdf", "The file does not contain a valid PDF header.")
    if suffix == ".txt":
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError(
                422, "invalid_encoding", "Text documents must use UTF-8 encoding."
            ) from exc
        if "\x00" in content or not content.strip():
            raise AppError(422, "invalid_text", "The text document contains no usable text.")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 10000
                    or sum(item.file_size for item in entries) > 100 * 1024 * 1024
                ):
                    raise AppError(413, "expanded_file_too_large", "Expanded DOCX is too large.")
                if "word/document.xml" not in archive.namelist():
                    raise AppError(415, "invalid_docx", "The file is not a Word document.")
                if any(i.filename.endswith("vbaProject.bin") for i in entries):
                    raise AppError(
                        415, "unsupported_macros", "Macro-enabled documents are unsupported."
                    )
        except zipfile.BadZipFile as exc:
            raise AppError(415, "invalid_docx", "The Word document is corrupt.") from exc
