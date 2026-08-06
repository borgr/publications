"""The publications table: read, validate, write.

Format
------
`papers.csv` -- one row per paper, one column per field, UTF-8. It replaces
`Contributions_table.xlsx`, which caused three recurring problems:

  * Binary, so git could not diff it. Columns rotted invisibly: six were
    entirely empty when this migration ran, two of them duplicate headers, and
    one of those (`inter\\eval`) is read by build_bib to emit the \\UND tag --
    which therefore had never been emitted.
  * Addressed by *position* (`COL_PAPER = 26`, `row_data = [None] * 37`), so
    inserting a column silently wrote values into the wrong fields.
  * Excel holds an exclusive lock. Running the pipeline with the file open in
    Excel meant `wb.save()` fought the editor.

CSV keeps the spreadsheet workflow -- it opens in Excel, Numbers and
LibreOffice, and a wide grid of tag flags is still ticked across a row -- while
being diffable and addressable by header name.

The Excel risk that remains is round-trip mangling: Excel likes to add a BOM,
reformat numbers and coerce things to dates. `validate()` exists to make that
loud instead of silent, and runs on every load.

Migration is one-way but not destructive: `read_table()` prefers papers.csv and
falls back to the xlsx, so a fork that has not migrated keeps working.
"""

import csv
import os
import re

import pandas as pd

from identity import find_duplicate_titles

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(FILE_DIR, "papers.csv")
XLSX_PATH = os.path.join(FILE_DIR, "Contributions_table.xlsx")

# Columns the pipeline reads by name. Missing any of these is fatal -- the
# alternative is silently producing a CV with no venues or no tags.
REQUIRED_COLUMNS = ("Name", "Bib", "Venue", "Authors", "year", "Paper")

# Columns holding a 0/1 flag. Anything else in them is a validation error.
FLAG_COLUMNS = (
    "NLP", "Small Models", "Debating", "Recycling", "Scaling Laws",
    "Human-Model Interaction", "Efficient Pretraining Research", "Resources",
    "The Science of Deep Learning", "Methods", "Dataset", "Training",
    "Evaluation", "Shared-task\\effort", "Language&Cognition", "Open",
    "Meta-science", "Enabling Low Budget Research", "Efficiency", "Paper",
    "Workshop-paper", "Review, Survey and Position", "Not empty", "Other",
    "inter\\eval", "Allowing", "Efficient Evaluation",
)

SORT_COLUMN = "Time of publish ID"


class ValidationError(Exception):
    """Raised for a problem that makes the table unusable, not merely untidy."""


def _clean(value):
    """Normalize a cell to str/int/None, absorbing pandas' NaN and 'nan'."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    return text


def read_table(path=None, prefer_csv=True):
    """Load the publications table as a DataFrame.

    Prefers papers.csv, falling back to the xlsx so an unmigrated checkout still
    builds. Rows with no Name are dropped and the frame is sorted by publication
    order, matching the previous `read_df()` behaviour exactly.
    """
    if path:
        df = (pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
              if str(path).endswith(".csv") else pd.read_excel(path))
    elif prefer_csv and os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False, na_values=[""])
    else:
        df = pd.read_excel(XLSX_PATH)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValidationError(
            f"the publications table is missing required column(s): {missing}. "
            f"Found: {sorted(df.columns)[:12]}…")

    df = df.dropna(subset=["Name"])
    if SORT_COLUMN in df.columns:
        df[SORT_COLUMN] = pd.to_numeric(df[SORT_COLUMN], errors="coerce")
        df = df.sort_values(SORT_COLUMN, na_position="last")

    df["Name"] = df["Name"].apply(lambda v: str(v).strip())
    df["Bib"] = df["Bib"].apply(lambda v: str(v).strip() if pd.notna(v) else v)

    # Flags arrive as "1"/"1.0"/1 depending on the writer; normalize to int/None
    # so `row["Paper"].item()` and `val == 1` comparisons behave the same either
    # way. Unparseable values are left alone for validate() to report.
    for column in FLAG_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    if "year" in df.columns:
        df["year"] = pd.to_numeric(df["year"], errors="coerce")
    return df


def validate(df, strict=False):
    """Return a list of human-readable problems. Raises only if strict.

    Called on every load, because the failure this guards against -- Excel
    quietly reformatting a column on save -- produces a table that still parses
    and still builds, just wrongly.
    """
    problems = []

    for column in FLAG_COLUMNS:
        if column not in df.columns:
            continue
        bad = df[~df[column].isna() & ~df[column].isin([0, 1])]
        for _, row in bad.iterrows():
            problems.append(
                f"column {column!r} should hold 0, 1 or blank but has "
                f"{row[column]!r} on: {str(row['Name'])[:60]}")

    if "year" in df.columns:
        for _, row in df[df["year"].notna()].iterrows():
            year = row["year"]
            if not (1900 <= int(year) <= 2100):
                problems.append(f"implausible year {year!r} on: {str(row['Name'])[:60]}")

    keys = {}
    for _, row in df.iterrows():
        key = _clean(row.get("Bib"))
        if not key:
            continue
        if not re.fullmatch(r"[A-Za-z0-9:_\-./+]+", key):
            problems.append(f"BibTeX key {key!r} contains unusual characters")
        keys.setdefault(key, []).append(str(row.get("Name"))[:50])
    for key, names in keys.items():
        if len(names) > 1:
            problems.append(f"BibTeX key {key!r} is used by {len(names)} rows: {names}")

    for names in find_duplicate_titles(df["Name"].dropna()).values():
        problems.append(f"{len(names)} rows are the same paper: {names}")

    if strict and problems:
        raise ValidationError("; ".join(problems))
    return problems


def column_order(df):
    """Preserve the author's column order; append anything new at the end."""
    return list(df.columns)


def write_table(df, path=CSV_PATH):
    """Write papers.csv atomically, preserving column order.

    Flags are written as bare integers rather than "1.0", and blanks stay blank,
    so the file is stable across runs and its diffs stay readable.
    """
    tmp = path + ".tmp"
    columns = column_order(df)

    def fmt(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for _, row in df.iterrows():
                writer.writerow([fmt(row.get(c)) for c in columns])
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_rows(new_rows, path=CSV_PATH):
    """Append papers to the table by *column name*.

    Replaces the previous positional write (`row_data = [None] * 37` with
    hardcoded indices), which put values in the wrong fields whenever a column
    was inserted.
    """
    df = read_table(path if os.path.exists(path) else None)
    columns = column_order(df)
    next_id = 0
    if SORT_COLUMN in df.columns and df[SORT_COLUMN].notna().any():
        next_id = int(df[SORT_COLUMN].max())

    # Built as records rather than concatenating frames: pd.concat with all-NA
    # columns is deprecated and its dtype handling is scheduled to change, and a
    # table this size gains nothing from staying in pandas for the append.
    records = df.to_dict("records")
    added = 0
    for offset, row in enumerate(new_rows, start=1):
        record = {c: None for c in columns}
        record.update({k: v for k, v in row.items() if k in columns})
        if SORT_COLUMN in columns and not record.get(SORT_COLUMN):
            record[SORT_COLUMN] = next_id + offset
        records.append(record)
        added += 1

    write_table(pd.DataFrame(records, columns=columns), path)
    return added


def fill_blanks(by_column, path=CSV_PATH):
    """Fill blank cells: {column: {row_name: value}}. Returns cells written.

    Only ever writes where the cell is currently empty, so a value a human typed
    is never overwritten by a scraped one.
    """
    df = read_table(path if os.path.exists(path) else None)
    written = 0
    for column, assignments in by_column.items():
        if column not in df.columns or not assignments:
            continue
        for name, value in assignments.items():
            blank = df[column].isna() | (df[column].astype(str).str.strip() == "")
            mask = (df["Name"] == name) & blank
            if mask.any():
                df.loc[mask, column] = value
                written += int(mask.sum())
    if written:
        write_table(df, path)
    return written


def set_bib_keys(assignments, path=CSV_PATH):
    """Fill in the Bib cell for rows identified by exact Name. Returns the count."""
    df = read_table(path if os.path.exists(path) else None)
    written = 0
    for name, key in assignments.items():
        mask = (df["Name"] == name) & (df["Bib"].isna() | (df["Bib"] == ""))
        if mask.any():
            df.loc[mask, "Bib"] = key
            written += int(mask.sum())
    if written:
        write_table(df, path)
    return written
