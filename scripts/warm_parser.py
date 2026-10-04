"""Provision tokenizer and Docling/OCR assets using a supplied local PDF.

Usage: python scripts/warm_parser.py /path/to/small-scanned.pdf
The selected document is only processed locally; its text is not printed.
"""

import argparse
import json
from pathlib import Path

from docvault.llm.provider import token_count
from docvault.parsing import parse_document_file


def main() -> None:
    """Warm local parsing assets with a supplied document and print parser metadata only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    token_count("Warm the tokenizer cache.")
    mime = (
        "application/pdf"
        if arguments.path.suffix.lower() == ".pdf"
        else ("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    )
    parsed = parse_document_file(arguments.path, mime, arguments.path.name)
    print(
        json.dumps(
            {"parser": parsed.parser, "pages": parsed.page_count, "chunks": len(parsed.chunks)}
        )
    )


if __name__ == "__main__":
    main()
