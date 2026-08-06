#!/usr/bin/env python3
"""Remove duplicate rows from the publications table, keeping the published one.

A duplicate row is two rows whose titles normalize identically -- usually the
preprint spelling and the published spelling of one paper, entered at different
times. They are not cosmetic: both rows are emitted, so the CV lists the paper
twice, and both compete for the same Scholar citation count.

Which row survives is decided by `bib_utils.publication_rank`, the same rule
step 3 uses to avoid downgrading an entry and that build_bib uses to decide what
to emit -- so the three cannot disagree. An ACL @inproceedings beats the same
paper's arXiv @misc.

    python scripts/dedupe.py --dry-run
    python scripts/dedupe.py

Rows whose BibTeX entry cannot be found are never dropped: with nothing to rank
they cannot be compared, so they are reported instead.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bib_utils import choose_published, parse_bibtex, publication_rank  # noqa: E402
from identity import find_duplicate_titles, normalize_title  # noqa: E402
from table_io import read_table, write_table  # noqa: E402

BIB_PATH = os.path.join(ROOT, "orig.bib")


def plan(df, bib_text):
    """Return (drops, unresolved) where drops is [(loser_row, winner_row, why)]."""
    by_key = {e["item_name"]: e for e in parse_bibtex(bib_text)}
    drops, unresolved = [], []

    for names in find_duplicate_titles(df["Name"].dropna()).values():
        candidates = []
        for name in names:
            cell = df[df["Name"] == name]["Bib"]
            key = str(cell.iloc[0]).strip() if len(cell) else ""
            entry = by_key.get(key) if key and key.lower() not in ("nan", "none") else None
            candidates.append((name, key, entry))

        rankable = [(n, k, e) for n, k, e in candidates if e is not None]
        if len(rankable) < 2:
            unresolved.append([(n, k, e is not None) for n, k, e in candidates])
            continue

        winner_entry, loser_entries = choose_published([e for _, _, e in rankable])
        winner = next(c for c in rankable if c[2] is winner_entry)
        for loser_entry in loser_entries:
            loser = next(c for c in rankable if c[2] is loser_entry)
            drops.append((loser, winner,
                          f"@{loser_entry['type']} rank {publication_rank(loser_entry)} "
                          f"< @{winner_entry['type']} rank {publication_rank(winner_entry)}"))
    return drops, unresolved


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df = read_table()
    with open(BIB_PATH) as f:
        bib_text = f.read()

    drops, unresolved = plan(df, bib_text)

    if unresolved:
        print(f"{len(unresolved)} duplicate group(s) cannot be ranked "
              f"(no BibTeX entry to compare):")
        for group in unresolved:
            for name, key, has_entry in group:
                mark = "entry" if has_entry else "no entry"
                print(f"  [{key or '(no key)':<40}] {mark:<8} {name[:56]}")
        print("  Resolve these by hand, or let step 3 assign an entry first.\n")

    if not drops:
        print("No duplicate rows to remove.")
        return 0

    print(f"{len(drops)} duplicate row(s) to remove:")
    for (loser_name, loser_key, _), (winner_name, winner_key, _), why in drops:
        print(f"  keep   [{winner_key}] {winner_name[:62]}")
        print(f"  remove [{loser_key}] {loser_name[:62]}")
        print(f"         because {why}")

    if args.dry_run:
        print("\n(dry-run: table not modified)")
        return 0

    drop_names = {loser[0] for loser, _, _ in drops}
    before = len(df)
    kept = df[~df["Name"].isin(drop_names)]
    write_table(kept)
    print(f"\nRemoved {before - len(kept)} row(s) → papers.csv now has {len(kept)}.")
    print("The dropped rows' BibTeX entries are left in orig.bib; they are simply "
          "no longer referenced, and step 3 will not resurrect them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
