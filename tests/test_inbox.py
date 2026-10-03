from pathlib import Path

from tars import inbox


def test_meaningful_filename_becomes_the_title():
    assert inbox.title_for(Path("snowflake_export-cadence.md"), "body") == "snowflake export cadence"


def test_dateish_or_generic_filename_falls_back_to_first_line():
    assert inbox.title_for(Path("2026-10-03 0915.md"), "\n# Call with Ana\nmore") == "Call with Ana"
    assert inbox.title_for(Path("Untitled.txt"), "x" * 80) == "x" * 60
