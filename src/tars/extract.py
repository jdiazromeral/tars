"""Normalize a capture target (URL, file, pasted text) into markdown text."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) tars/0.1"

TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".org", ".csv", ".json", ".yaml", ".yml"}


@dataclass
class Extracted:
    text: str
    title: str | None = None
    meta: dict = field(default_factory=dict)
    source_bytes: bytes | None = None
    source_ext: str | None = None


class ExtractionError(Exception):
    pass


def from_url(url: str) -> Extracted:
    response = httpx.get(url, follow_redirects=True, timeout=30,
                         headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        extracted = from_pdf_bytes(response.content)
        extracted.meta["url"] = url
        return extracted
    return _from_html(response.text, url=url)


def from_file(path: Path) -> Extracted:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        extracted = from_pdf_bytes(path.read_bytes())
    elif suffix in {".html", ".htm"}:
        extracted = _from_html(path.read_text(errors="replace"))
        extracted.source_bytes = path.read_bytes()
        extracted.source_ext = "html"
    elif suffix in TEXT_EXTENSIONS or not suffix:
        extracted = Extracted(text=path.read_text(errors="replace"))
    else:
        raise ExtractionError(f"unsupported file type: {path.name}")
    extracted.title = extracted.title or path.stem
    extracted.meta["path"] = str(path.resolve())
    return extracted


def from_pdf_bytes(data: bytes) -> Extracted:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise ExtractionError("PDF contains no extractable text")
    title = None
    if reader.metadata and reader.metadata.title:
        title = str(reader.metadata.title)
    return Extracted(text=text, title=title, meta={"pages": len(reader.pages)},
                     source_bytes=data, source_ext="pdf")


def _from_html(html: str, url: str | None = None) -> Extracted:
    import trafilatura

    text = trafilatura.extract(html, url=url, output_format="markdown",
                               include_links=False, include_tables=True)
    title = None
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        title = metadata.title if metadata else None
    except Exception:
        pass
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = match.group(1).strip() if match else None
    if not text:
        raise ExtractionError("could not extract readable content from HTML")
    meta = {"url": url} if url else {}
    return Extracted(text=text, title=title, meta=meta)
