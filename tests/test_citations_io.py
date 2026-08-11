"""citations.csv reading and writing, including the legacy shape."""

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from citations_io import (
    HEADER,
    STATS_FILE,
    days_since_fetch,
    fetched_date,
    read_citation_rows,
    write_citation_rows,
)

LEGACY = (
    "Title,Cited by,Year\n"
    "Findings of the BabyLM challenge,379,2023\n"
    "A Warstadt and L Choshen,,\n"
    "CoNLL,,\n"
    "TextArena,51,2025\n"
    "L Guertler,,\n"
    "arXiv,,\n"
)


def test_reads_the_legacy_three_row_shape(tmp_path):
    """A fork that has not re-fetched yet must still build."""
    path = tmp_path / "citations.csv"
    path.write_text(LEGACY, encoding="utf-8")
    papers = read_citation_rows(str(path))
    assert [p["title"] for p in papers] == ["Findings of the BabyLM challenge", "TextArena"]
    assert papers[0]["citations"] == 379
    assert papers[0]["authors"] == "A Warstadt and L Choshen"
    assert papers[0]["venue"] == "CoNLL"
    assert papers[1]["citations"] == 51


def test_round_trips_the_current_shape(tmp_path):
    path = str(tmp_path / "citations.csv")
    original = [{"title": "TextArena", "citations": 51, "year": "2025",
                 "authors": "L Guertler", "venue": "arXiv",
                 "scholar_id": "8b8IhUYAAAAJ:RHpTSmoSYBkC"}]
    write_citation_rows(original, path)
    assert read_citation_rows(path) == original


def test_header_is_written(tmp_path):
    path = str(tmp_path / "c.csv")
    write_citation_rows([], path)
    assert open(path).read().strip().split(",") == HEADER


def test_missing_count_is_none_not_zero(tmp_path):
    """Scholar omits the cell for uncited papers; that is not a count of zero."""
    path = str(tmp_path / "c.csv")
    write_citation_rows([{"title": "New Paper", "citations": None}], path)
    assert read_citation_rows(path)[0]["citations"] is None


def test_merged_record_marker_is_stripped(tmp_path):
    """Scholar writes '12*' for counts that include merged versions."""
    path = tmp_path / "c.csv"
    path.write_text("Title,Cited by,Year,Authors,Venue,Scholar ID\nP,12*,2024,A,V,U:1\n",
                    encoding="utf-8")
    assert read_citation_rows(str(path))[0]["citations"] == 12


def test_thousands_separator_is_handled(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("Title,Cited by,Year,Authors,Venue,Scholar ID\nP,\"1,110\",2023,A,V,U:1\n",
                    encoding="utf-8")
    assert read_citation_rows(str(path))[0]["citations"] == 1110


def test_absent_file_reads_as_empty(tmp_path):
    assert read_citation_rows(str(tmp_path / "nope.csv")) == []


def test_titles_with_commas_and_quotes_survive_a_round_trip(tmp_path):
    """The CSV migration's core risk: quoting must be lossless."""
    path = str(tmp_path / "c.csv")
    tricky = 'Q2: Evaluating "Factual" Consistency, Fully'
    write_citation_rows([{"title": tricky, "citations": 3}], path)
    assert read_citation_rows(path)[0]["title"] == tricky


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "c.csv")
    write_citation_rows([{"title": "P", "citations": 1}], path)
    assert not os.path.exists(path + ".tmp")


def test_failed_write_does_not_destroy_the_previous_file(tmp_path):
    """An interrupted fetch must not leave a truncated citations.csv."""
    path = str(tmp_path / "c.csv")
    write_citation_rows([{"title": "Good", "citations": 1}], path)

    class Explode:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    try:
        write_citation_rows([Explode()], path)
    except Exception:
        pass
    assert "Good" in open(path).read()
    assert not os.path.exists(path + ".tmp")


# ── how old the counts are ───────────────────────────────────────────────────
# Read from the recorded date rather than from a file mtime, which `git clone`
# resets: in CI and in a fork every file was modified at checkout.

def _stats(tmp_path, **payload):
    (tmp_path / STATS_FILE).write_text(json.dumps(payload), encoding="utf-8")
    return str(tmp_path)


def test_the_recorded_fetch_date_is_read_back(tmp_path):
    root = _stats(tmp_path, citations=9, fetched="2026-08-10")
    assert fetched_date(root) == "2026-08-10"


def test_days_are_counted_from_that_date(tmp_path):
    root = _stats(tmp_path, fetched=(date.today() - timedelta(days=45)).isoformat())
    assert days_since_fetch(root) == 45


def test_today_s_fetch_is_zero_days_old_not_none(tmp_path):
    """None means "unknown" everywhere it is used, so a fresh fetch must not
    report it -- that would silence the staleness check rather than pass it."""
    root = _stats(tmp_path, fetched=date.today().isoformat())
    assert days_since_fetch(root) == 0


def test_a_date_in_the_future_is_not_negative_days(tmp_path):
    """A clock set wrong, or a file from another machine. Negative days would read
    as fresh forever, which is the one answer that cannot be right."""
    root = _stats(tmp_path, fetched=(date.today() + timedelta(days=3)).isoformat())
    assert days_since_fetch(root) == 0


def test_stats_without_a_date_are_unknown_rather_than_old(tmp_path):
    """The state of every fork: init_new_author zeroes the file and writes no date."""
    root = _stats(tmp_path, citations=0, h_index=0)
    assert fetched_date(root) == ""
    assert days_since_fetch(root) is None


def test_a_missing_stats_file_is_unknown(tmp_path):
    assert fetched_date(str(tmp_path)) == ""
    assert days_since_fetch(str(tmp_path)) is None


def test_an_unreadable_stats_file_is_unknown_not_an_exception(tmp_path):
    """A fetch killed mid-write leaves this half-written. Building the CV must not
    stop because the date of the numbers cannot be read."""
    (tmp_path / STATS_FILE).write_text('{"citations": 9', encoding="utf-8")
    assert days_since_fetch(str(tmp_path)) is None


def test_a_date_that_is_not_a_date_is_unknown(tmp_path):
    root = _stats(tmp_path, fetched="last Tuesday")
    assert fetched_date(root) == "last Tuesday"   # reported verbatim if printed
    assert days_since_fetch(root) is None
