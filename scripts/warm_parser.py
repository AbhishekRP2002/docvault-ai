"""Provision tokenizer and Docling/OCR assets using a supplied local PDF.

Usage: python scripts/warm_parser.py /path/to/small-scanned.pdf
The selected document is only processed locally; its text is not printed.
"""

import argparse
import json
from pathlib import Path

from docvault.ai.parsing import parse_file
from docvault.ai.provider import token_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    token_count("Warm the tokenizer cache.")
    mime = (
        "application/pdf"
        if arguments.path.suffix.lower() == ".pdf"
        else ("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    )
    parsed = parse_file(arguments.path, mime, arguments.path.name)
    print(
        json.dumps(
            {"parser": parsed.parser, "pages": parsed.page_count, "chunks": len(parsed.chunks)}
        )
    )


if __name__ == "__main__":
    main()
