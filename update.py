#!/usr/bin/env python3
"""One-command update for the publications pipeline.

Steps:
  1. Refresh citations.csv from Google Scholar
  2. Add new papers (in Scholar, not in xlsx) to Contributions_table.xlsx
  3. Resolve arXiv entries in orig.bib to published BibTeX (in-place);
     also resolve xlsx entries with no Bib key and add them to orig.bib.

Usage:
    python update.py [--dry-run] [--skip-fetch] [--skip-xlsx] [--skip-resolve]
"""

import argparse
import csv
import difflib
import os
import re
import subprocess
import sys
import time

import openpyxl

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FILE_DIR)

from resolve_arxiv import (
    _PUBLISHED_SOURCES,
    _get_arxiv_id,
    gen_key,
    get_arxiv_entries,
    resolve,
    update_bib_inplace,
)

CITATIONS_CSV = os.path.join(FILE_DIR, "citations.csv")
XLSX_PATH     = os.path.join(FILE_DIR, "Contributions_table.xlsx")
BIB_PATH      = os.path.join(FILE_DIR, "orig.bib")

# xlsx column indices (0-based matching ws.iter_rows)
COL_ID      = 0   # Time of publish ID
COL_VENUE   = 2
COL_NAME    = 3
COL_BIB     = 4
COL_AUTHORS = 5
COL_YEAR    = 6
COL_PAPER   = 26


# ── Helpers ────────────────────────────────────────────────────────────────────

def _simplify(text: str) -> str:
    return re.sub(r'[\W_]+', '', text.lower().strip())


def _parse_scholar_csv(csv_path: str) -> list:
    """Parse citations.csv (3-row-per-paper) into list of dicts."""
    papers = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    i = 1  # skip header row
    while i < len(rows):
        r0 = rows[i]     if i     < len(rows) else []
        r1 = rows[i + 1] if i + 1 < len(rows) else []
        r2 = rows[i + 2] if i + 2 < len(rows) else []
        title   = r0[0].strip() if r0 else ""
        year    = r0[2].strip() if len(r0) > 2 else ""
        authors = r1[0].strip() if r1 else ""
        venue   = r2[0].strip() if r2 else ""
        if title:
            papers.append({"title": title, "year": year, "authors": authors, "venue": venue})
        i += 3
    return papers


def _get_max_id(ws) -> int:
    max_id = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = row[COL_ID]
        if isinstance(v, (int, float)) and v > 0:
            max_id = max(max_id, int(v))
    return max_id


def _xlsx_names(ws) -> list:
    """Return list of simplified Name strings from the xlsx."""
    result = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = row[COL_NAME]
        if name:
            result.append(_simplify(str(name)))
    return result


# ── Step 1 ─────────────────────────────────────────────────────────────────────

def step1_fetch(dry_run: bool) -> None:
    print("[Step 1] Fetching Scholar profile → citations.csv")
    if dry_run:
        print("  (dry-run: skipped)")
        return
    subprocess.run(
        [sys.executable, os.path.join(FILE_DIR, "fetch_citations.py")],
        check=True,
    )
    print("  Done.")


# ── Step 2 ─────────────────────────────────────────────────────────────────────

def step2_add_new_papers(dry_run: bool) -> None:
    print("\n[Step 2] Checking for new papers not in Contributions_table.xlsx")
    wb     = openpyxl.load_workbook(XLSX_PATH)
    ws     = wb.active
    papers = _parse_scholar_csv(CITATIONS_CSV)
    known  = _xlsx_names(ws)

    new_papers = [
        p for p in papers
        if not difflib.get_close_matches(_simplify(p["title"]), known, n=1, cutoff=0.85)
    ]
    if not new_papers:
        print("  No new papers found.")
        return

    print(f"  {len(new_papers)} new paper(s):")
    max_id = _get_max_id(ws)
    for i, p in enumerate(new_papers, 1):
        year_val = int(p["year"]) if p["year"].isdigit() else p["year"]
        print(f"    [{year_val}] {p['title'][:70]}")
        if not dry_run:
            row_data = [None] * 37
            row_data[COL_ID]      = max_id + i
            row_data[COL_VENUE]   = p["venue"]
            row_data[COL_NAME]    = p["title"]
            row_data[COL_AUTHORS] = p["authors"]
            row_data[COL_YEAR]    = year_val
            row_data[COL_PAPER]   = 1
            ws.append(row_data)

    if not dry_run:
        wb.save(XLSX_PATH)
        print(f"  Saved {len(new_papers)} new row(s) to Contributions_table.xlsx")
    else:
        print("  (dry-run: xlsx not modified)")


# ── Step 3 ─────────────────────────────────────────────────────────────────────

def _get_xlsx_missing(bib_text: str) -> list:
    """Return xlsx rows with no Bib key (or key absent from bib), with gen_key applied."""
    from augment_bib import parse_bibtex as _parse_bib
    try:
        from augment_bib import read_df
        df = read_df()
    except Exception as exc:
        print(f"  Warning: could not read xlsx: {exc}", file=sys.stderr)
        return []
    existing_keys = {e["item_name"] for e in _parse_bib(bib_text)}
    missing = []
    for _, row in df.iterrows():
        bib_key = str(row.get("Bib", "")).strip()
        name    = str(row.get("Name", "")).strip()
        authors = str(row.get("Authors", "") or "").strip()
        if authors.lower() == "nan":
            authors = ""
        year    = str(int(row.get("year", 0) or 0))
        if not name:
            continue
        key_missing = not bib_key or bib_key.lower() in ("nan", "none") or bib_key not in existing_keys
        if key_missing:
            new_key = gen_key(authors, year, name) if authors else f"unknown{year}{_simplify(name)[:10]}"
            missing.append({"title": name, "authors": authors, "year": year,
                            "item_name": new_key, "content": ""})
    return missing


def step3_resolve(dry_run: bool) -> None:
    print("\n[Step 3] Resolving BibTeX entries and updating orig.bib")
    with open(BIB_PATH) as f:
        bib_text = f.read()

    # Part A: existing arXiv entries in orig.bib
    arxiv_entries = get_arxiv_entries(bib_text)
    print(f"  {len(arxiv_entries)} arXiv entries to check in orig.bib...")
    updates = []
    for entry in arxiv_entries:
        key      = entry["item_name"]
        arxiv_id = _get_arxiv_id(entry)
        label    = f"arXiv:{arxiv_id}" if arxiv_id else "(no arXiv ID)"
        print(f"    [{key[:40]}] {label:<22}", end=" ", flush=True)
        bib, source = resolve(entry["title"], arxiv_id, key, entry.get("content", ""))
        print(f"→ {source}")
        updates.append((key, bib, source))
        time.sleep(0.5)

    # Part B: xlsx entries with no Bib key
    missing_entries = _get_xlsx_missing(bib_text)
    new_entries = []
    if missing_entries:
        print(f"\n  {len(missing_entries)} xlsx entry(ies) with no BibTeX key...")
    for entry in missing_entries:
        key = entry["item_name"]
        print(f"    [{key:<40}]", end=" ", flush=True)
        bib, source = resolve(entry["title"], None, key, "")
        print(f"→ {source}")
        if bib:
            new_entries.append((key, bib))
        time.sleep(0.5)

    # Summary before writing
    upgraded = sum(1 for _, _, src in updates if src in _PUBLISHED_SOURCES)
    print(f"\n  {upgraded}/{len(arxiv_entries)} arXiv entries have a published version")
    print(f"  {len(new_entries)} new entries to append to orig.bib")

    if dry_run:
        print("  (dry-run: orig.bib not modified)")
        return

    new_bib_text, n_replaced, n_appended = update_bib_inplace(bib_text, updates, new_entries)
    with open(BIB_PATH, "w") as f:
        f.write(new_bib_text)
    print(f"  Replaced {n_replaced} entries, appended {n_appended} entries → orig.bib updated")

    # Write resolved Bib keys back into xlsx for previously-missing entries
    if new_entries:
        wb = openpyxl.load_workbook(XLSX_PATH)
        ws = wb.active
        key_map = {bib: key for key, bib in new_entries}  # won't work; use list
        resolved_keys = {key for key, _ in new_entries}
        for row in ws.iter_rows(min_row=2):
            name_cell = row[COL_NAME]
            bib_cell  = row[COL_BIB]
            authors_cell = row[COL_AUTHORS]
            year_cell = row[COL_YEAR]
            if not name_cell.value or bib_cell.value:
                continue
            name    = str(name_cell.value).strip()
            authors = str(authors_cell.value or "").strip()
            year    = str(int(year_cell.value) if isinstance(year_cell.value, (int, float)) else year_cell.value or 0)
            candidate_key = gen_key(authors, year, name) if authors else None
            if candidate_key and candidate_key in resolved_keys:
                bib_cell.value = candidate_key
        wb.save(XLSX_PATH)
        print(f"  Updated Bib keys in Contributions_table.xlsx")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Update the publications pipeline end-to-end")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying files")
    parser.add_argument("--skip-fetch",   action="store_true", help="Skip step 1 (citations.csv fetch)")
    parser.add_argument("--skip-xlsx",    action="store_true", help="Skip step 2 (xlsx update)")
    parser.add_argument("--skip-resolve", action="store_true", help="Skip step 3 (bib resolve)")
    args = parser.parse_args()

    if not args.skip_fetch:
        step1_fetch(args.dry_run)
    else:
        print("[Step 1] Skipped.")

    if not args.skip_xlsx:
        step2_add_new_papers(args.dry_run)
    else:
        print("\n[Step 2] Skipped.")

    if not args.skip_resolve:
        step3_resolve(args.dry_run)
    else:
        print("\n[Step 3] Skipped.")


if __name__ == "__main__":
    main()
