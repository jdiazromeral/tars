import io

import pytest

from tars import extract, ingest


def test_note_is_content_addressed_and_origin_overrides():
    a = ingest.resolve_target("-", stdin=io.StringIO("  same words\n"))
    b = ingest.resolve_target("-", stdin=io.StringIO("same words"))
    assert a.connector == "note"
    assert a.origin == b.origin == ingest.note_origin("same words")
    slot = ingest.resolve_target("-", stdin=io.StringIO("x"), origin="note:todo")
    assert slot.origin == "note:todo"


def test_empty_stdin_is_an_extraction_error():
    with pytest.raises(extract.ExtractionError, match="stdin was empty"):
        ingest.resolve_target("-", stdin=io.StringIO("   \n"))


def test_file_identity_is_its_bytes_not_its_path(tmp_path):
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    first.write_text("hello")
    second.write_text("hello")
    a, b = ingest.resolve_target(str(first)), ingest.resolve_target(str(second))
    assert a.connector == "file"
    assert a.origin == b.origin
    assert a.extracted.meta["path"] != b.extracted.meta["path"]


def test_missing_file_is_an_extraction_error(tmp_path):
    with pytest.raises(extract.ExtractionError, match="no such file"):
        ingest.resolve_target(str(tmp_path / "nope.md"))


def test_web_pdf_keeps_no_sidecar(monkeypatch):
    monkeypatch.setattr(extract, "from_url", lambda url: extract.Extracted(
        text="body", source_bytes=b"%PDF", source_ext="pdf"))
    resolved = ingest.resolve_target("https://example.com/paper.pdf?utm_source=x")
    assert resolved.connector == "web"
    assert resolved.origin == "https://example.com/paper.pdf"
    assert resolved.extracted.source_bytes is None
    assert resolved.extracted.source_ext is None
