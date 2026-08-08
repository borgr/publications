"""The publications table: schema validation and name-addressed writes.

The validator's job is Excel silently reformatting a column on save. A mangled
table still parses and still builds -- just wrongly -- so it has to be checked,
not trusted.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import table_io
from table_io import (
    ValidationError,
    append_rows,
    fill_blanks,
    read_table,
    set_bib_keys,
    set_column,
    validate,
    write_table,
)

HEADER = ("Time of publish ID,Venue,Name,Bib,Authors,year,Paper,Workshop-paper,"
          '"Review, Survey and Position",Open\n')
ROWS = ("1,acl,A Real Paper,doe2024real,\"Doe, Jane\",2024,1,,,1\n"
        "2,tacl,Another Paper,doe2023another,\"Doe, Jane\",2023,1,,,\n")


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "papers.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    return str(path)


# ── reading ──────────────────────────────────────────────────────────────────

def test_reads_and_sorts_by_publication_order(csv_path):
    df = read_table(csv_path)
    assert list(df["Name"]) == ["A Real Paper", "Another Paper"]


def test_rows_without_a_name_are_dropped(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + ROWS + "3,acl,,nokey,,2024,1,,,\n", encoding="utf-8")
    assert len(read_table(str(path))) == 2


def test_missing_required_column_is_fatal(tmp_path):
    """Better to fail loudly than emit a CV with no venues."""
    path = tmp_path / "p.csv"
    path.write_text("Name,Bib\nA Paper,k\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        read_table(str(path))
    assert "Venue" in str(excinfo.value)


def test_flags_are_numeric_regardless_of_how_they_were_written(tmp_path):
    """Excel writes 1.0, csv writers write 1, both must compare equal to 1."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,P,k,\"Doe, J\",2024,1.0,,,\n", encoding="utf-8")
    assert read_table(str(path))["Paper"].iloc[0] == 1


def test_titles_with_commas_and_quotes_survive(tmp_path):
    tricky = 'Q2: Evaluating "Factual" Consistency, Fully'
    df = pd.DataFrame([{"Time of publish ID": 1, "Venue": "acl", "Name": tricky,
                        "Bib": "k", "Authors": "Doe, J", "year": 2024, "Paper": 1}])
    path = str(tmp_path / "p.csv")
    write_table(df, path)
    assert read_table(path)["Name"].iloc[0] == tricky


@pytest.mark.parametrize("value", [None, float("nan"), "", "   ", "nan", "None"])
def test_the_several_spellings_of_an_empty_cell_all_read_as_empty(value):
    """A cell is blank in six different ways depending on whether pandas, Excel,
    csv or a str() round-trip last touched it. Everything downstream branches on
    emptiness, so they have to collapse to one value."""
    assert table_io._clean(value) is None


@pytest.mark.parametrize("value, expected", [(2024, "2024"), (" acl ", "acl"),
                                             ("Not A Number", "Not A Number")])
def test_a_populated_cell_reads_as_stripped_text(value, expected):
    assert table_io._clean(value) == expected


# ── validation ───────────────────────────────────────────────────────────────

def test_non_binary_flag_is_reported(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,P,k,\"Doe, J\",2024,2,,,\n", encoding="utf-8")
    problems = validate(read_table(str(path)))
    assert any("'Paper'" in p and "2" in p for p in problems)


def test_blank_flag_is_fine(csv_path):
    assert not any("Workshop-paper" in p for p in validate(read_table(csv_path)))


def test_duplicate_bib_key_is_reported(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,First,same,\"Doe, J\",2024,1,,,\n"
                             "2,acl,Second,same,\"Doe, J\",2024,1,,,\n", encoding="utf-8")
    assert any("used by 2 rows" in p for p in validate(read_table(str(path))))


def test_duplicate_title_is_reported(tmp_path):
    """The live table has exactly this, and it puts one paper in the CV twice."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,A Real Paper,k1,\"Doe, J\",2024,1,,,\n"
                             "2,acl,a real paper,k2,\"Doe, J\",2024,1,,,\n", encoding="utf-8")
    assert any("are the same paper" in p for p in validate(read_table(str(path))))


def test_implausible_year_is_reported(tmp_path):
    """Excel loves coercing things into dates."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,P,k,\"Doe, J\",45231,1,,,\n", encoding="utf-8")
    assert any("implausible year" in p for p in validate(read_table(str(path))))


def test_a_bib_key_with_unusual_characters_is_reported(tmp_path):
    """A space or a brace in the key produces a `\\cite` LaTeX cannot resolve, and
    the CV builds with an unresolved reference rather than an error."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + '1,acl,P,"doe 2024 real","Doe, J",2024,1,,,\n',
                    encoding="utf-8")
    assert any("unusual characters" in p for p in validate(read_table(str(path))))


def test_a_key_with_the_punctuation_real_keys_use_is_accepted(tmp_path):
    """DOI-derived and hyphenated keys are normal, so flagging them would make the
    report noise."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + '1,acl,P,"doe-2024.real_v2/a+b","Doe, J",2024,1,,,\n',
                    encoding="utf-8")
    assert not any("unusual characters" in p for p in validate(read_table(str(path))))


def test_rows_with_no_key_yet_are_not_reported_as_sharing_one(tmp_path):
    """Every paper step 2 adds starts with an empty Bib cell, so on a normal run
    several rows have no key at all. Counting those together would report a
    duplicate key on every run and bury the real ones."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + '1,acl,First,,"Doe, J",2024,1,,,\n'
                             '2,acl,Second,,"Doe, J",2024,1,,,\n', encoding="utf-8")
    assert not any("BibTeX key" in p for p in validate(read_table(str(path))))


def test_clean_table_validates_silently(csv_path):
    assert validate(read_table(csv_path)) == []


def test_strict_mode_raises(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,P,k,\"Doe, J\",2024,7,,,\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        validate(read_table(str(path)), strict=True)


# ── writing by name ──────────────────────────────────────────────────────────

def test_append_writes_by_column_name_not_position(csv_path):
    """The positional write it replaces misfiled values whenever a column moved."""
    append_rows([{"Name": "Fresh Paper", "Venue": "emnlp", "year": 2026,
                  "Paper": 1, "Authors": "Roe, R"}], csv_path)
    df = read_table(csv_path)
    row = df[df["Name"] == "Fresh Paper"].iloc[0]
    assert row["Venue"] == "emnlp"
    assert row["Authors"] == "Roe, R"
    assert row["Paper"] == 1
    assert row["year"] == 2026


def test_append_assigns_the_next_publication_id(csv_path):
    append_rows([{"Name": "Fresh Paper"}], csv_path)
    df = read_table(csv_path)
    assert df[df["Name"] == "Fresh Paper"]["Time of publish ID"].iloc[0] == 3


def test_append_preserves_existing_rows_and_columns(csv_path):
    before = read_table(csv_path)
    append_rows([{"Name": "Fresh Paper"}], csv_path)
    after = read_table(csv_path)
    assert list(after.columns) == list(before.columns)
    assert len(after) == len(before) + 1
    assert after[after["Name"] == "A Real Paper"]["Bib"].iloc[0] == "doe2024real"


def test_append_ignores_unknown_fields(csv_path):
    append_rows([{"Name": "Fresh Paper", "NotAColumn": "x"}], csv_path)
    assert "NotAColumn" not in read_table(csv_path).columns


def test_set_bib_keys_fills_only_empty_cells(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,Has Key,existing,\"Doe, J\",2024,1,,,\n"
                             "2,acl,Needs Key,,\"Doe, J\",2024,1,,,\n", encoding="utf-8")
    written = set_bib_keys({"Needs Key": "new2024key", "Has Key": "should_not_apply"},
                           str(path))
    df = read_table(str(path))
    assert written == 1
    assert df[df["Name"] == "Needs Key"]["Bib"].iloc[0] == "new2024key"
    assert df[df["Name"] == "Has Key"]["Bib"].iloc[0] == "existing"


def test_set_bib_keys_with_no_matches_does_not_rewrite(tmp_path, csv_path):
    before = open(csv_path).read()
    assert set_bib_keys({"No Such Paper": "k"}, csv_path) == 0
    assert open(csv_path).read() == before


# ── filling and overwriting cells ────────────────────────────────────────────

def test_fill_blanks_writes_only_where_the_cell_is_empty(tmp_path):
    """Every value it fills is scraped, so a cell a human typed outranks it."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,acl,Has Venue,k1,\"Doe, J\",2024,1,,,\n"
                             "2,,Needs Venue,k2,\"Doe, J\",2024,1,,,\n", encoding="utf-8")
    written = fill_blanks({"Venue": {"Needs Venue": "emnlp",
                                     "Has Venue": "should_not_apply"}}, str(path))
    df = read_table(str(path))
    assert written == 1
    assert df[df["Name"] == "Needs Venue"]["Venue"].iloc[0] == "emnlp"
    assert df[df["Name"] == "Has Venue"]["Venue"].iloc[0] == "acl"


def test_fill_blanks_treats_whitespace_as_empty(tmp_path):
    """A cell Excel left holding a space is blank to a reader, so it must be
    blank here too -- otherwise the paper keeps no venue and no report says why."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + '1,"   ",Spacey,k,"Doe, J",2024,1,,,\n', encoding="utf-8")
    assert fill_blanks({"Venue": {"Spacey": "acl"}}, str(path)) == 1


def test_fill_blanks_fills_several_columns_in_one_pass(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(HEADER + '1,,Bare,k,,2024,1,,,\n', encoding="utf-8")
    assert fill_blanks({"Venue": {"Bare": "acl"},
                        "Authors": {"Bare": "Doe, J"}}, str(path)) == 2


def test_fill_blanks_ignores_a_column_the_table_does_not_have(csv_path):
    """A forked table may not carry every optional column, and a caller asking
    for one is not a reason to fail the run."""
    assert fill_blanks({"NotAColumn": {"A Real Paper": "x"}}, csv_path) == 0


def test_fill_blanks_with_nothing_to_do_does_not_rewrite(csv_path):
    before = open(csv_path).read()
    assert fill_blanks({"Venue": {"No Such Paper": "acl"}}, csv_path) == 0
    assert open(csv_path).read() == before


def test_set_column_replaces_a_value_that_is_already_there(tmp_path):
    """The one caller is the preprint-to-published venue upgrade: the row says
    ArXiv, the new BibTeX entry says ACL, and the CV section has to follow."""
    path = tmp_path / "p.csv"
    path.write_text(HEADER + "1,arxiv,Upgraded,k,\"Doe, J\",2024,1,,,\n",
                    encoding="utf-8")
    assert set_column("Venue", {"Upgraded": "acl"}, str(path)) == 1
    assert read_table(str(path))["Venue"].iloc[0] == "acl"


def test_set_column_leaves_rows_it_was_not_given_alone(csv_path):
    set_column("Venue", {"A Real Paper": "emnlp"}, csv_path)
    df = read_table(csv_path)
    assert df[df["Name"] == "Another Paper"]["Venue"].iloc[0] == "tacl"


def test_set_column_on_an_absent_column_is_a_no_op(csv_path):
    before = open(csv_path).read()
    assert set_column("NotAColumn", {"A Real Paper": "x"}, csv_path) == 0
    assert open(csv_path).read() == before


def test_set_column_with_no_assignments_is_a_no_op(csv_path):
    assert set_column("Venue", {}, csv_path) == 0


def test_set_column_matches_the_name_exactly(csv_path):
    """Fuzzy matching here would overwrite a *different* paper's venue, which no
    later step would notice or report."""
    assert set_column("Venue", {"a real paper": "emnlp"}, csv_path) == 0


# ── writing ──────────────────────────────────────────────────────────────────

def test_write_leaves_no_temp_file(csv_path):
    write_table(read_table(csv_path), csv_path)
    assert not os.path.exists(csv_path + ".tmp")


def test_flags_round_trip_as_integers_not_floats(csv_path):
    """`1.0` in the file makes diffs noisy and confuses the validator."""
    write_table(read_table(csv_path), csv_path)
    assert ",1.0," not in open(csv_path).read()


def test_a_write_that_fails_partway_leaves_the_table_untouched(csv_path, monkeypatch):
    """papers.csv is the repository's only copy of the author's bibliography, and
    the run that writes it is unattended. Writing in place would truncate it to
    however many rows got flushed before the error; the temp-file-then-rename is
    what makes a crash cost nothing."""
    before = open(csv_path).read()
    df = read_table(csv_path)

    class Exploding:
        def writerow(self, _row):
            raise OSError("disk full")

    monkeypatch.setattr(table_io.csv, "writer", lambda _f: Exploding())
    with pytest.raises(OSError):
        write_table(df, csv_path)
    assert open(csv_path).read() == before
    assert not os.path.exists(csv_path + ".tmp"), "a stale temp file was left behind"


# ── the live table ───────────────────────────────────────────────────────────

def test_live_table_loads_and_has_the_required_columns():
    df = read_table()
    assert len(df) > 50
    for column in ("Name", "Bib", "Venue", "Authors", "year", "Paper"):
        assert column in df.columns


def test_every_key_in_the_table_resolves_to_an_entry():
    """A row's Bib key must name an entry that exists.

    orig.bib and identity.json are both keyed on this string, so a row pointing at
    a key nothing defines produces a CV with a silently absent paper. build_bib
    only warns, and a warning in a weekly unattended log is not a guard.

    A row with no key at all is a different state -- a paper step 3 has not
    resolved yet -- and is WORKLIST.md's business.
    """
    from bib_utils import parse_bibtex
    with open(os.path.join(table_io.FILE_DIR, "orig.bib"), encoding="utf-8") as fh:
        defined = {e["item_name"] for e in parse_bibtex(fh.read())}

    df = read_table()
    rows = ((name, str(raw).strip())
            for name, raw in zip(df["Name"], df["Bib"].fillna("")))
    dangling = sorted(
        f"{key} (row {name!r})" for name, key in rows
        if key and key.lower() not in ("nan", "none") and key not in defined)
    assert not dangling, (
        "table rows name BibTeX keys that orig.bib does not define:\n  "
        + "\n  ".join(dangling))
