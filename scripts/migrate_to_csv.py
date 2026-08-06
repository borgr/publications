#!/usr/bin/env python3
"""One-time migration: Contributions_table.xlsx -> papers.csv.

Conservative by design. It preserves every column and every value, because a
migration that also tidies is a migration you cannot verify. The only structural
change is merging the duplicate headers that pandas disambiguates with a `.1`
suffix -- keeping both is meaningless, since they came from two columns with the
same name in the sheet.

Everything else it finds is *reported*, not changed: entirely empty columns,
columns the code reads that hold no data, and anything validate() objects to.
Deciding what to delete is yours.

    python scripts/migrate_to_csv.py --dry-run
    python scripts/migrate_to_csv.py

The xlsx is left in place. `read_table()` prefers papers.csv once it exists, so
the switch happens the moment this writes the file, and reverting means deleting
papers.csv.
"""

import argparse
import os
import re
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_bib  # noqa: E402
from table_io import (CSV_PATH, REQUIRED_COLUMNS, XLSX_PATH,  # noqa: E402
                      read_table, validate, write_table)

# Columns build_bib reads by name. If one of these is empty, a feature is dead.
_CODE_READ_COLUMNS = tuple(build_bib.RELEVANT_TAGS.keys()) + (
    "Paper", "Workshop-paper", "Review, Survey and Position", "Venue", "Name",
    "Bib", "Authors", "year",
)

_DUP_SUFFIX_RE = re.compile(r"^(.*)\.(\d+)$")


def merge_duplicate_headers(df):
    """Collapse pandas' `X.1` columns back into `X`. Returns (df, [notes])."""
    notes = []
    for column in list(df.columns):
        match = _DUP_SUFFIX_RE.match(str(column))
        if not match:
            continue
        base = match.group(1)
        if base not in df.columns:
            continue
        dup_filled = int(df[column].notna().sum())
        base_filled = int(df[base].notna().sum())
        # Prefer the base column's value; take the duplicate's only where blank.
        df[base] = df[base].where(df[base].notna(), df[column])
        df = df.drop(columns=[column])
        notes.append(f"merged duplicate header {column!r} into {base!r} "
                     f"({base_filled} + {dup_filled} non-empty values)")
    return df, notes


def report_empty_columns(df):
    empty = [c for c in df.columns if df[c].notna().sum() == 0]
    dead_features = [c for c in empty if c in _CODE_READ_COLUMNS]
    return empty, dead_features


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", default=CSV_PATH)
    args = parser.parse_args()

    if not os.path.exists(XLSX_PATH):
        print(f"No xlsx at {XLSX_PATH}", file=sys.stderr)
        return 1
    if os.path.exists(args.output) and not args.dry_run:
        print(f"{args.output} already exists — refusing to overwrite. "
              f"Delete it first if you really want to re-migrate.", file=sys.stderr)
        return 1

    raw = pd.read_excel(XLSX_PATH)
    print(f"Read {XLSX_PATH}")
    print(f"  {raw.shape[0]} rows x {raw.shape[1]} columns")

    df, notes = merge_duplicate_headers(raw)
    for note in notes:
        print(f"  {note}")

    empty, dead = report_empty_columns(df)
    if empty:
        print(f"\n{len(empty)} column(s) are entirely empty. Kept as-is; delete "
              f"any you no longer want:")
        for column in empty:
            print(f"  - {column!r}")
    if dead:
        print(f"\nWARNING: {len(dead)} empty column(s) are read by build_bib, so "
              f"the feature they drive is inert:")
        for column in dead:
            tag = build_bib.RELEVANT_TAGS.get(column)
            because = f" (would emit the {tag} tag)" if tag else ""
            print(f"  - {column!r}{because}")

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        print(f"\nFATAL: required column(s) absent: {missing}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\n(dry-run: {args.output} not written)")
        return 0

    write_table(df, args.output)
    print(f"\nWrote {args.output}")

    # Read it back and check it against the xlsx, value by value.
    reloaded = read_table(args.output)
    original = read_table(XLSX_PATH, prefer_csv=False)
    print(f"  reloaded: {reloaded.shape[0]} rows x {reloaded.shape[1]} columns")

    problems = validate(reloaded)
    if problems:
        print(f"\n{len(problems)} validation note(s):")
        for problem in problems:
            print(f"  - {problem}")

    mismatches = compare(original, reloaded)
    if mismatches:
        print(f"\nWARNING: {len(mismatches)} value(s) differ after the round trip:")
        for line in mismatches[:20]:
            print(f"  - {line}")
        return 1
    print("\nRound-trip check: every value matches the xlsx. ✓")
    print("papers.csv is now the source of truth (read_table prefers it).")
    return 0


def compare(before, after):
    """Compare two frames cell by cell on shared columns, keyed by Name."""
    out = []
    shared = [c for c in before.columns if c in after.columns]
    after_by_name = {str(r["Name"]): r for _, r in after.iterrows()}
    for _, row in before.iterrows():
        name = str(row["Name"])
        other = after_by_name.get(name)
        if other is None:
            out.append(f"row missing after migration: {name[:60]}")
            continue
        for column in shared:
            a, b = row[column], other[column]
            if pd.isna(a) and pd.isna(b):
                continue
            if pd.isna(a) != pd.isna(b) or str(a).strip() != str(b).strip():
                out.append(f"{name[:40]!r} column {column!r}: {a!r} -> {b!r}")
    return out


if __name__ == "__main__":
    sys.exit(main())
