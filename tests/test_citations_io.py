"""citations.csv reading and writing, including the legacy shape."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from citations_io import HEADER, read_citation_rows, write_citation_rows

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
