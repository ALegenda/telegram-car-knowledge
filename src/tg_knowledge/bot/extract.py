"""Bounded document extraction in a disposable process; never executes attachments."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..knowledge import attachment_text, normalize

MAX_CHARACTERS = 200_000


def extract(path: Path) -> dict:
    sections, count, limited = [], 0, False
    if path.suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        if len(reader.pages) > 300:
            return {"status": "pdf_over_300_pages", "sections": []}
        for number, page in enumerate(reader.pages, 1):
            text = normalize(page.extract_text() or "")
            remaining = MAX_CHARACTERS - count
            if len(text) > remaining:
                text, limited = text[:remaining], True
            if text:
                sections.append([f"PDF, страница {number}", text])
                count += len(text)
            if limited:
                break
        status = "partial_text_limit" if limited else "extracted" if sections else "needs_ocr"
    else:
        text, status = attachment_text(path)
        if len(text) > MAX_CHARACTERS:
            text, status = text[:MAX_CHARACTERS], "partial_text_limit"
        if text:
            sections = [["Документ", text]]
        elif status.startswith("extracted"):
            status = "no_text"
    return {"status": status, "sections": sections}


def main():
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    try:
        result = extract(Path(sys.argv[1]))
    except Exception as exc:
        result = {"status": "extraction_error:" + type(exc).__name__, "sections": []}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
