import io

import pytest

from tars.extract import ExtractionError, _from_html, from_pdf_bytes


def _make_pdf_bytes(text: str, title: str | None = None) -> bytes:
    """Hand-roll a minimal single-page PDF with a Tj text-showing operator.

    No reportlab dependency in this project, so pypdf can't author a PDF for
    us either — this is the smallest structure pypdf's parser (and its xref
    fallback) will accept.
    """
    content = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    info = f"<< /Title ({title}) >>".encode() if title else None
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    if info is not None:
        objects.append(info)

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(b"%d 0 obj\n" % i)
        buf.write(obj)
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(b"xref\n")
    buf.write(b"0 %d\n" % (len(objects) + 1))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(b"%010d 00000 n \n" % off)
    trailer = b"<< /Size %d /Root 1 0 R" % (len(objects) + 1)
    if info is not None:
        trailer += b" /Info %d 0 R" % len(objects)
    trailer += b" >>\n"
    buf.write(b"trailer\n")
    buf.write(trailer)
    buf.write(b"startxref\n%d\n%%%%EOF" % xref_offset)
    return buf.getvalue()


def test_from_pdf_bytes_extracts_text():
    extracted = from_pdf_bytes(_make_pdf_bytes("Hello World"))
    assert "Hello World" in extracted.text
    assert extracted.meta["pages"] == 1
    assert extracted.source_ext == "pdf"
    assert extracted.source_bytes == _make_pdf_bytes("Hello World")


def test_from_pdf_bytes_reads_title_from_metadata():
    extracted = from_pdf_bytes(_make_pdf_bytes("Hello World", title="My PDF Title"))
    assert extracted.title == "My PDF Title"


def test_from_pdf_bytes_no_title_metadata_leaves_title_none():
    extracted = from_pdf_bytes(_make_pdf_bytes("Hello World"))
    assert extracted.title is None


def test_from_pdf_bytes_raises_on_no_extractable_text():
    with pytest.raises(ExtractionError):
        from_pdf_bytes(_make_pdf_bytes(""))


def test_from_pdf_bytes_raises_on_garbage_bytes():
    with pytest.raises(Exception):
        from_pdf_bytes(b"not a pdf at all")


HTML_ARTICLE = """<html><head><title>My Page Title</title></head>
<body>
<article>
<h1>My Page Title</h1>
<p>This is the first paragraph with enough content to be considered
meaningful text by trafilatura's extraction heuristics, which tend to
discard very short snippets of only a few words.</p>
<p>This is a second paragraph, also reasonably long, to make sure the
extractor keeps both paragraphs in the resulting markdown output for this
test fixture.</p>
</article>
</body></html>"""


def test_from_html_extracts_markdown_and_title():
    extracted = _from_html(HTML_ARTICLE)
    assert "My Page Title" in extracted.text
    assert "first paragraph" in extracted.text
    assert extracted.title == "My Page Title"


def test_from_html_records_url_in_meta():
    extracted = _from_html(HTML_ARTICLE, url="https://example.com/page")
    assert extracted.meta["url"] == "https://example.com/page"


def test_from_html_no_url_omits_url_from_meta():
    extracted = _from_html(HTML_ARTICLE)
    assert "url" not in extracted.meta


def test_from_html_falls_back_to_title_tag_when_metadata_extraction_fails(monkeypatch):
    import trafilatura

    monkeypatch.setattr(trafilatura, "extract_metadata", lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    extracted = _from_html(HTML_ARTICLE)
    assert extracted.title == "My Page Title"


def test_from_html_raises_on_no_extractable_content():
    with pytest.raises(ExtractionError):
        _from_html("<html><head><title>Empty</title></head><body></body></html>")
