"""The publications table: schema validation and name-addressed writes.

The validator exists because of the one real risk the CSV migration introduces:
Excel silently reformatting a column on save. A mangled table still parses and
still builds -- just wrongly -- so it has to be checked, not trusted.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import table_io
from table_io import (ValidationError, append_rows, read_table, set_bib_keys,
                      validate, write_table)

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


def test_write_leaves_no_temp_file(csv_path):
    write_table(read_table(csv_path), csv_path)
    assert not os.path.exists(csv_path + ".tmp")


def test_flags_round_trip_as_integers_not_floats(csv_path):
    """`1.0` in the file makes diffs noisy and confuses the validator."""
    write_table(read_table(csv_path), csv_path)
    assert ",1.0," not in open(csv_path).read()


# ── the live table ───────────────────────────────────────────────────────────

def test_live_table_loads_and_has_the_required_columns():
    df = read_table()
    assert len(df) > 50
    for column in ("Name", "Bib", "Venue", "Authors", "year", "Paper"):
        assert column in df.columns


def test_csv_invents_no_rows_the_xlsx_never_had():
    """The migration may only lose deliberately-removed rows, never add any.

    Not an equality check: scripts/dedupe.py removes duplicate rows from the CSV
    while the xlsx keeps its pre-migration contents, so the two legitimately
    differ in size. What must never happen is a row appearing in the CSV that
    was not in the source -- that would mean the migration, or step 2, invented
    one.
    """
    if not os.path.exists(table_io.CSV_PATH) or not os.path.exists(table_io.XLSX_PATH):
        pytest.skip("both formats not present")
    from_csv = read_table()
    from_xlsx = read_table(prefer_csv=False)

    from identity import normalize_title
    csv_titles = {normalize_title(n) for n in from_csv["Name"]}
    xlsx_titles = {normalize_title(n) for n in from_xlsx["Name"]}
    assert csv_titles <= xlsx_titles, (
        f"rows in papers.csv with no counterpart in the xlsx: "
        f"{csv_titles - xlsx_titles}")

    # Every BibTeX key the CSV carries must match the xlsx's for the same paper.
    csv_keys = {normalize_title(n): (k or "") for n, k in
                zip(from_csv["Name"], from_csv["Bib"].fillna(""))}
    xlsx_keys = {normalize_title(n): (k or "") for n, k in
                 zip(from_xlsx["Name"], from_xlsx["Bib"].fillna(""))}
    for title, key in csv_keys.items():
        if key and xlsx_keys.get(title):
            assert key == xlsx_keys[title], f"key changed for {title}"
