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

from bib_utils import (_entry_year, choose_published, is_preprint,  # noqa: E402
                       parse_bibtex, publication_rank)
from identity import (IdentityStore, duplicate_groups_by_identifier,  # noqa: E402
                      find_duplicate_titles, normalize_title)
from table_io import read_table, write_table  # noqa: E402

BIB_PATH = os.path.join(ROOT, "orig.bib")


def plan(df, bib_text):
    """Return (drops, unresolved) where drops is [(loser_row, winner_row, why)]."""
    by_key = {e["item_name"]: e for e in parse_bibtex(bib_text)}
    drops, unresolved = [], []

    # Group by identifier first: it catches retitled duplicates that title
    # comparison misses, and it is a stronger claim about sameness.
    name_by_key = {}
    for _, row in df.iterrows():
        key = str(row.get("Bib") or "").strip()
        if key and key.lower() not in ("nan", "none"):
            name_by_key[key] = str(row.get("Name") or "")
    groups = []
    for keys in duplicate_groups_by_identifier(IdentityStore.load(), set(name_by_key)):
        groups.append([name_by_key[k] for k in keys if k in name_by_key])
    groups.extend(find_duplicate_titles(df["Name"].dropna()).values())

    for names in groups:
        if len(names) < 2:
            continue
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
            drops.append((loser, winner, _why(winner_entry, loser_entry)))
    return drops, unresolved


def _why(winner, loser):
    """State the reason the winner won, in the terms actually used to decide it.

    Must mirror `choose_published`'s ordering. Printing only the publication rank
    produced the self-contradicting "@article rank 10 < @misc rank -12" once year
    became the tiebreaker between two preprints.
    """
    if is_preprint(loser) and not is_preprint(winner):
        return (f"@{winner['type']} is the published version, "
                f"@{loser['type']} is a preprint")
    if is_preprint(winner) and is_preprint(loser):
        wy, ly = _entry_year(winner), _entry_year(loser)
        if wy != ly:
            return (f"both are preprints and {wy} is the current version "
                    f"(the other is {ly})")
    return (f"@{winner['type']} ranks {publication_rank(winner)} vs "
            f"@{loser['type']} at {publication_rank(loser)}")


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
