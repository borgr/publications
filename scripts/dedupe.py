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

from bib_utils import (
    _entry_year,
    choose_published,
    is_preprint,
    parse_bibtex,
    publication_rank,
)
from citations_io import read_citation_rows
from identity import (
    IdentityStore,
    duplicate_groups_by_any_known_title,
    duplicate_groups_by_bib_identifier,
    duplicate_groups_by_identifier,
    find_duplicate_titles,
    join_citations,
    merge_overlapping_groups,
)
from table_io import read_table, write_table

BIB_PATH = os.path.join(ROOT, "orig.bib")
CITATIONS_CSV = os.path.join(ROOT, "citations.csv")


def citation_effect(df, drop_names):
    """What happens to citation counts if `drop_names` are removed.

    Returns (carried, orphaned). A removed row may be the only title a Scholar
    record matches, in which case its citations would land nowhere -- so the
    merge is checked before it is made, rather than trusting that the survivor
    happens to match too.

    Where both rows match records, `join_citations` sums them, which is what
    Scholar itself shows once the versions are merged.
    """
    rows = read_citation_rows(CITATIONS_CSV)
    if not rows:
        return {}, []
    store = IdentityStore.load()
    before = join_citations(rows, sorted(set(df["Name"].dropna())), store=store)
    kept = df[~df["Name"].isin(drop_names)]
    after = join_citations(rows, sorted(set(kept["Name"].dropna())), store=store)

    total_before = sum(v for v in before.matched.values() if v)
    total_after = sum(v for v in after.matched.values() if v)
    orphaned = [(t, v) for t, v in after.unmatched
                if (t, v) not in before.unmatched and v]
    return {"before": total_before, "after": total_after}, orphaned


def bind_dropped_scholar_ids(df, drops):
    """Bind a dropped row's Scholar ID to the surviving paper's key.

    Makes the merge permanent: the next fetch attributes that Scholar record to
    the survivor by identifier, so its citations follow the merge even though the
    title it was matched on no longer exists in the table.
    """
    rows = read_citation_rows(CITATIONS_CSV)
    if not rows:
        return 0
    store = IdentityStore.load()
    join = join_citations(rows, sorted(set(df["Name"].dropna())), store=store)
    bound = 0
    for (loser_name, _lk, _le), (_wn, winner_key, _we), _why in drops:
        source = join.source.get(loser_name) or {}
        scholar_id = str(source.get("scholar_id") or "").strip()
        if scholar_id and winner_key:
            store.record(winner_key, title=loser_name, scholar_id=scholar_id)
            bound += 1
    if bound:
        store.save()
    return bound


def plan(df, bib_text):
    """Return (drops, unresolved, suspected).

    `drops` is [(loser_row, winner_row, why)] and is safe to apply: every group in
    it is held together by an identifier or by identical titles, which is proof
    that two rows are one paper.

    `suspected` is [[(name, key), ...]] -- groups that only a title crossing
    connects. Those are reported rather than applied, because the same crossing
    arises from a *mis-resolution*, and then the entries genuinely describe two
    different works so ranking them picks a winner on the strength of the wrong
    one's metadata. That happened: a Scholar record for a Nature paper was added
    as a second row, resolved to an unrelated ISAIM talk by one of its authors,
    and `choose_published` preferred the talk -- newer, and with a booktitle.
    """
    by_key = {e["item_name"]: e for e in parse_bibtex(bib_text)}
    drops, unresolved = [], []

    # Group by identifier first: it catches retitled duplicates that title
    # comparison misses, and it is a stronger claim about sameness.
    name_by_key = {}
    for _, row in df.iterrows():
        key = str(row.get("Bib") or "").strip()
        if key and key.lower() not in ("nan", "none"):
            name_by_key[key] = str(row.get("Name") or "")
    keys_in_use = set(name_by_key)
    store = IdentityStore.load()

    def _names(keys):
        return [name_by_key[k] for k in keys if k in name_by_key]

    groups = []
    for keys in duplicate_groups_by_identifier(store, keys_in_use):
        groups.append(_names(keys))
    # And from the entries themselves, which need no accumulated state to be
    # right. The store misses any paper whose entry never went through
    # resolution, and four papers were reaching the CV twice because of it.
    for keys in duplicate_groups_by_bib_identifier(bib_text, keys_in_use):
        groups.append(_names(keys))
    groups.extend(find_duplicate_titles(df["Name"].dropna()).values())
    # One paper, one decision: the detectors overlap by design, and two
    # overlapping groups can otherwise each nominate a different survivor.
    groups = merge_overlapping_groups(groups)

    # Reported, never applied -- see the docstring. Anything the evidence above
    # already covers is dropped from the report rather than raised twice.
    proven = {n for g in groups for n in g}
    suspected = []
    for keys in duplicate_groups_by_any_known_title(name_by_key, bib_text):
        names = _names(keys)
        if len(names) > 1 and not set(names) & proven:
            suspected.append([(n, k) for n, k in zip(names, keys)])

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
        winner_entry, loser_entries, tiebreak = _prefer_the_cited_row(
            store, rankable, winner_entry, loser_entries)
        winner = next(c for c in rankable if c[2] is winner_entry)
        for loser_entry in loser_entries:
            loser = next(c for c in rankable if c[2] is loser_entry)
            drops.append((loser, winner,
                          tiebreak or _why(winner_entry, loser_entry)))
    return drops, unresolved, suspected


def _prefer_the_cited_row(store, rankable, winner_entry, loser_entries):
    """Among entries `choose_published` ranks equally, keep the cited one.

    A tie means the two entries describe the same version of the same paper, so
    which one survives is bookkeeping rather than a claim about the publication --
    and `choose_published` breaks it on content length, which is arbitrary: a DBLP
    record carries `bibsource` and `timestamp` boilerplate and so wins by about
    twenty bytes over the identical hand-written entry.

    That is the wrong way round. The row whose key has a Scholar ID bound is the
    one citations reach the paper through; dropping it means rebinding the ID onto
    the survivor and hoping the rebind holds. Here it did not: keeping the DBLP
    rows for MuLER and the two machine-translation papers would have orphaned 15
    citations, which `citation_effect` then refused outright -- a correct refusal,
    but it left the duplicates in place with no way forward.

    Returns (winner, losers, reason) where reason is None unless this function
    changed the outcome -- `_why` can only report the rank comparison, which for a
    tie prints the uninformative "@inproceedings ranks 85 vs @inproceedings at 85".
    """
    tied = [e for e in [winner_entry] + list(loser_entries)
            if is_preprint(e) == is_preprint(winner_entry)
            and publication_rank(e) == publication_rank(winner_entry)
            and _entry_year(e) == _entry_year(winner_entry)]
    if len(tied) < 2:
        return winner_entry, loser_entries, None

    def key_of(entry):
        return next((k for _, k, e in rankable if e is entry), "")

    def cited(entry):
        return bool((store.records.get(key_of(entry)) or {}).get("scholar_id"))

    preferred = next((e for e in tied if cited(e)), None)
    if preferred is None or preferred is winner_entry:
        return winner_entry, loser_entries, None
    losers = [e for e in [winner_entry] + list(loser_entries) if e is not preferred]
    return preferred, losers, (
        f"the records are equivalent (@{preferred['type']}, same year, same "
        f"rank), and [{key_of(preferred)}] is the one Scholar citations are "
        f"bound to")


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

    drops, unresolved, suspected = plan(df, bib_text)

    if suspected:
        print(f"{len(suspected)} group(s) look like one paper but are not "
              f"provably so — not removing them:")
        for group in suspected:
            for name, key in group:
                print(f"  [{key or '(no key)':<40}] {name[:56]}")
            print("  One row is named what the other's BibTeX entry is titled. "
                  "That is either\n  a duplicate, or a row resolved to the wrong "
                  "paper — check the entries before\n  removing either row.\n")

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

    totals, orphaned = citation_effect(df, drop_names)
    if orphaned:
        print(f"\nREFUSING to remove: {len(orphaned)} Scholar record(s) would "
              f"lose their only matching row, and their citations with them:")
        for title, value in orphaned:
            print(f"    {value} citations — {title[:64]}")
        print("  Give the surviving row a title these also match, or merge the "
              "records in your Scholar profile first.")
        return 1
    if totals:
        print(f"\nCitations: {totals['before']} before, {totals['after']} after "
              f"— {'unchanged' if totals['before'] == totals['after'] else 'CHANGED'}")

    bound = bind_dropped_scholar_ids(df, drops)
    if bound:
        print(f"Bound {bound} dropped row(s)' Scholar ID to the surviving paper, "
              f"so their citations follow the merge.")

    before = len(df)
    kept = df[~df["Name"].isin(drop_names)]
    write_table(kept)
    print(f"\nRemoved {before - len(kept)} row(s) → papers.csv now has {len(kept)}.")
    print("The dropped rows' BibTeX entries are left in orig.bib; they are simply "
          "no longer referenced, and step 3 will not resurrect them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
