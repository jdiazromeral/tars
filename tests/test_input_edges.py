"""Input edge cases (review theme I): what tars is handed must never be
silently mangled, and anything it can't take is left where it was."""

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from click.testing import CliRunner

from tars import normalize, store
from tars.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


# --- encodings ---------------------------------------------------------------

def test_sweep_keeps_a_file_that_is_not_utf8(root):
    # It used to decode with U+FFFD replacements and then delete the original:
    # "café ñandú" became "caf� �and�" with no copy of the true bytes left.
    path, runner = root
    (path / "inbox/latin.txt").write_bytes("café ñandú".encode("latin-1"))
    (path / "inbox/ok.txt").write_text("fine")
    result = runner.invoke(main, ["sweep"])
    assert result.exit_code == 0, result.output
    assert (path / "inbox/latin.txt").read_bytes() == "café ñandú".encode("latin-1")
    assert "latin.txt" in result.output and "UTF-8" in result.output
    assert not (path / "inbox/ok.txt").exists()
    assert not any("�" in p.read_text() for p in path.glob("raw/note/*.md"))


def test_add_refuses_a_file_that_is_not_utf8(root, tmp_path_factory):
    path, runner = root
    f = tmp_path_factory.mktemp("src") / "latin.txt"
    f.write_bytes("café ñandú".encode("latin-1"))
    result = runner.invoke(main, ["add", str(f)])
    assert result.exit_code != 0
    assert "UTF-8" in result.output
    assert not list(path.glob("raw/*/*.md"))


# --- line endings --------------------------------------------------------------

def _tars(path, *args, stdin: bytes = b""):
    # The real entry point: CliRunner's text stdin turns \r\n into \n on its
    # own, so a CRLF test through it would never send a CR at all.
    tars = Path(sys.executable).parent / "tars"
    return subprocess.run([str(tars), *args], input=stdin, capture_output=True,
                          env={**os.environ, "TARS_HOME": str(path)})


def test_crlf_text_is_stored_with_lf_and_stays_in_sync(root):
    path, runner = root
    result = _tars(path, "add", "-", "--title", "crlf", stdin=b"a\r\nb\rc")
    assert result.returncode == 0, result.stderr
    doc_id = result.stdout.split()[1].decode()
    assert b"\r" not in (path / "raw/note/crlf.md").read_bytes()
    assert store.read_raw(path / "raw/note/crlf.md").text == "a\nb\nc"
    assert runner.invoke(main, ["doctor"]).exit_code == 0  # no permanent drift
    hash_before = _hash(path, doc_id)
    runner.invoke(main, ["tag", doc_id, "--concept", "x"])
    assert _hash(path, doc_id) == hash_before  # shelving never changes content


def _hash(path, doc_id):
    from tars import db as database
    return database.connect(path).execute(
        "SELECT content_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]


def test_the_same_note_with_either_line_ending_is_one_note(root):
    path, _ = root
    _tars(path, "add", "-", stdin=b"line one\nline two")
    result = _tars(path, "add", "-", stdin=b"line one\r\nline two")
    assert result.stdout.startswith(b"unchanged"), result.stdout
    assert len(list(path.glob("raw/note/*.md"))) == 1


# --- unicode forms -------------------------------------------------------------

def _add_titled(path, title, body):
    # A real subprocess: CliRunner hands argv through Python unchanged, but the
    # NFC/NFD question is about what reaches the filesystem.
    result = _tars(path, "add", "-", "--title", title, stdin=body.encode())
    return result


@pytest.mark.parametrize("title", ["한글 문서", "Reunión semanal"])
def test_nfc_and_nfd_titles_never_overwrite_each_other(root, title):
    # On APFS the two forms name the same file; the second capture used to
    # replace the first (Hangul) or mangle the slug (reunio-n).
    path, _ = root
    first = _add_titled(path, unicodedata.normalize("NFC", title), "first body")
    second = _add_titled(path, unicodedata.normalize("NFD", title), "second body")
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    bodies = sorted(store.read_raw(p).text for p in path.glob("raw/note/*.md"))
    assert bodies == ["first body", "second body"]
    for p in path.glob("raw/note/*.md"):
        assert unicodedata.is_normalized("NFC", p.stem)
        assert "-n-" not in p.stem and not p.stem.startswith("reunio-")


def test_slugify_composes_before_slugging():
    assert store.slugify(unicodedata.normalize("NFD", "Reunión semanal")) == "reunión-semanal"


# --- normalize leaves URLs alone ----------------------------------------------

def test_vocab_rules_never_rewrite_inside_a_url(tmp_path):
    (tmp_path / "vocab.yml").write_text("Acme:\n  variants: [acne]\n")
    rules = normalize.load_rules(tmp_path)
    text = "see https://acne.com/acne/x and <https://acne.io|acne docs>, then acne"
    assert normalize.apply(text, rules) == \
        "see https://acne.com/acne/x and <https://acne.io|Acme docs>, then Acme"


# --- slack DMs -------------------------------------------------------------------

def test_slack_select_requires_the_channel_type(root):
    path, runner = root
    (path / "connectors.yml").write_text("slack:\n  channels: [C0AAA111]\n")
    result = runner.invoke(main, ["slack", "select", "--channel", "C0AAA111"], input="[]")
    assert result.exit_code != 0
    assert "--channel-type" in result.output


@pytest.mark.parametrize("label", ["public_channel", "private_channel", "mpim"])
def test_a_dm_id_is_refused_whatever_its_label(root, label):
    path, runner = root
    (path / "connectors.yml").write_text(
        "slack:\n  channels: [D0ABC123]\n  include_group_dms: true\n")
    result = runner.invoke(main, ["slack", "select", "--channel", "D0ABC123",
                                  "--channel-type", label],
                           input=json.dumps([{"ts": "1.0", "reply_count": 3}]))
    assert result.exit_code != 0
    assert "1:1 DM" in result.output


# --- review of #16 -------------------------------------------------------------

def test_a_legacy_nfd_named_file_is_never_overwritten(root):
    # A file named from an NFD title before slugify composed to NFC: APFS says
    # the NFC name exists, but the stems compare unequal as strings. Any
    # existing file that isn't ours must push the new capture to a suffix.
    path, runner = root
    legacy = path / "raw/note" / (unicodedata.normalize("NFD", "한글-문서") + ".md")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("---\nid: aaaaaaaaaaaa\nconnector: note\norigin: note:legacy\n"
                      "title: old\nconcepts: []\n---\n\nlegacy words\n")
    result = _add_titled(path, "한글 문서", "new words")
    assert result.returncode == 0, result.stderr
    bodies = sorted(p.read_text().rsplit("\n\n", 1)[-1].strip()
                    for p in path.glob("raw/note/*.md"))
    assert bodies == ["legacy words", "new words"]


@pytest.mark.parametrize("text", [
    "<mailto:bob@acne.com|bob@acne.com>",
    "write to bob@acne.com today",
    "see www.acne.com/acne",
    "acnehttps://x.com",          # no word boundary before the URL: not a match
])
def test_vocab_rules_never_rewrite_an_address(tmp_path, text):
    (tmp_path / "vocab.yml").write_text("Acme:\n  variants: [acne]\n")
    assert normalize.apply(text, normalize.load_rules(tmp_path)) == text


def test_a_utf8_bom_is_dropped_on_the_way_in(root, tmp_path_factory):
    path, runner = root
    (path / "inbox/2026-10-04.txt").write_bytes("﻿Meeting notes\nbody".encode("utf-8"))
    runner.invoke(main, ["sweep"])
    f = tmp_path_factory.mktemp("src") / "bom.txt"
    f.write_bytes("﻿Other notes".encode("utf-8"))
    runner.invoke(main, ["add", str(f)])
    for p in path.glob("raw/*/*.md"):
        doc = store.read_raw(p)
        assert not doc.text.startswith("﻿") and not (doc.title or "").startswith("﻿")
