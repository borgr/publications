import os
import re
from datetime import datetime
from typing import NamedTuple

import pandas as pd

from bib_utils import find_duplicate_keys, normalize_text, parse_bibtex, read_df
from citations_io import read_citation_rows
from identity import IdentityStore, find_duplicate_titles, join_citations
from venues import Venues

FILE_DIR = os.path.dirname(os.path.abspath(__file__))

RELEVANT_TAGS = {
    r"inter\eval": "\\UND",
    "Enabling Low Budget Research": "\\META",
    "Open": "\\COL",
    "Language&Cognition": "\\LANG",
}

_VENUES = Venues.load()
JOURNALS = _VENUES.journals
CONFERENCES = _VENUES.conferences

_VENUE_SPLIT_RE = re.compile(r'[2\-*^(]')

_CATEGORY_LABELS = {
    "journals":    "Journals",
    "conferences": "Conferences",
    "reviews":     "Reviews",
    "workshops":   "Workshop Articles",
    "drafts":      "ArXiv Articles",
}


class BibCategories(NamedTuple):
    journals: list
    conferences: list
    reviews: list
    workshops: list
    drafts: list


def extract_tags_str(row):
    tags = [RELEVANT_TAGS[col] for col, val in zip(row.index, row)
            if val == 1 and col in RELEVANT_TAGS]
    return "".join(tags)


def remove_pretitle_tags(s):
    return re.sub(r'\s*pretitle\s*=[^}]*},\s*', '', s)


def shorten_booktitle(s):
    return re.sub(r'(\s*booktitle\s*=.*?\d{4})(.*?)(}\s*,\s*\n)', r'\1\3', s, flags=re.DOTALL)


def simplify_venue(name):
    """Reduce a raw venue string to a venues.yaml key."""
    if pd.isna(name) or not name:
        return ""
    s = str(name).lower()
    alias = _VENUES.alias_for(s)
    if alias:
        return alias
    return _VENUE_SPLIT_RE.split(s, maxsplit=1)[0].strip()


def load_citations(citations_path):
    """Read citations.csv into row dicts, warning if the data has gone stale."""
    if not os.path.exists(citations_path):
        print(f"Warning: {citations_path} not found. Proceeding with empty citations.")
        return []
    mtime = datetime.fromtimestamp(os.path.getmtime(citations_path))
    age_days = (datetime.now() - mtime).days
    if age_days > 30:
        print(f"Warning: {citations_path} is {age_days} days old. Consider running update.py.")
    return read_citation_rows(citations_path)


def _build_name2cite(citation_rows, df_names, store=None):
    """Map publications-table paper names to citation counts.

    Delegates to identity.join_citations, which matches on the stable Scholar ID
    first and only then on titles. The previous implementation compared *raw*
    title strings with difflib, so BibTeX capitalization braces defeated it --
    "Findings of the {B}aby{LM} Challenge" never matched Scholar's plain
    spelling -- and an unconditional assignment let one paper's count overwrite
    another's. Two BabyLM papers holding 490 citations between them both
    reported 0 as a result.
    """
    result = join_citations(citation_rows, df_names, store=store)

    if result.needs_review:
        print("Citation counts matched by title rather than by Scholar ID "
              "(confirm, then a re-fetch will bind them exactly):")
        for name, incoming, tier, score in sorted(result.needs_review):
            print(f"  [{tier} {score:.0%}] table: {name[:64]}")
            if normalize_text(incoming) != normalize_text(name):
                print(f"{'':>21}scholar: {incoming[:64]}")
    if result.ambiguous:
        print("AMBIGUOUS: more than one Scholar row matched one table row. "
              "Both counts cannot be right — check for a duplicate row:")
        for name, cands in result.ambiguous:
            print(f"  table: {name[:66]}")
            for title, tier, score in cands:
                print(f"{'':>9}<- [{tier} {score:.0%}] {title[:60]}")
    if result.unmatched:
        print("Cited papers with no row in the publications table:")
        for title, value in result.unmatched:
            print(f"  {value if value is not None else '-':>6}  {title}")

    counts = result.tier_counts()
    print(f"Citation join: {len(result.matched)} matched "
          f"({', '.join(f'{n} {tier}' for tier, n in sorted(counts.items())) or 'none'}), "
          f"{len(result.unmatched)} unmatched")
    return result


def _categorize(venue_simple, is_arxiv, is_review, is_workshop):
    """Return the BibCategories field name for a paper, or None if unknown."""
    if is_arxiv:
        return "drafts"
    if is_review:
        return "reviews"
    if is_workshop:
        return "workshops"
    if venue_simple in JOURNALS:
        return "journals"
    if venue_simple in CONFERENCES:
        return "conferences"
    return None


def _process_entries(parsed, df, name2cite):
    """Build wzmn.bib text and categorise each paper.

    Returns (bib_out, BibCategories, bibs_seen, under_review_count, non_paper_count).
    """
    bib_parts = []
    cats = {field: [] for field in BibCategories._fields}
    bibs_seen = under_review = non_papers = 0

    for dic in parsed:
        matches = df[df["Bib"] == dic["item_name"]]
        if matches.shape[0] > 1:
            print(f"Warning: duplicate xlsx rows for bib key {dic['item_name']!r}, using first")
            matches = matches.iloc[:1]
        if matches.empty:
            continue

        row = matches
        beg = dic["beg"]
        rest = remove_pretitle_tags(shorten_booktitle(dic["rest"]))
        bibs_seen += 1

        if not row["Paper"].item():
            non_papers += 1
            bib_parts.append(beg + rest)
            continue

        row_bib = row["Bib"].item()
        venue_raw = row["Venue"].item() if pd.notna(row["Venue"].item()) else ""
        venue_simple = simplify_venue(venue_raw)
        is_arxiv = "xiv" in venue_raw.lower() or "review" in venue_raw.lower()
        is_workshop = row["Workshop-paper"].item() == 1
        is_review = row["Review, Survey and Position"].item() == 1

        tags = extract_tags_str(row.squeeze())
        # A matched paper with no count reported (None) renders as 0, same as an
        # unmatched one -- Scholar simply omits the cell for uncited papers.
        raw_count = name2cite.get(row["Name"].item())
        cite_count = str(raw_count if raw_count is not None else 0).replace("*", "").strip()
        rest = "\n    pretitle={" + tags + "}," + rest
        rest = "\n    citations={" + cite_count + "}," + rest

        if not is_arxiv and not is_workshop:
            venue_info = _VENUES.description(venue_simple)
            if venue_info:
                rest = "\n    venueinf={" + venue_info + "}," + rest
            elif venue_simple:
                print(f"Warning: unknown venue {venue_raw!r} (key: {venue_simple!r}) — venueinf omitted")

        category = _categorize(venue_simple, is_arxiv, is_review, is_workshop)
        if category is None:
            print(f"Warning: cannot categorize venue {venue_raw!r} for {row_bib!r}, adding to drafts")
            category = "drafts"
        cats[category].append(row_bib)
        if is_arxiv:
            under_review += 1

        bib_parts.append(beg + rest)

    bib_out = "".join(p + "\n\n" for p in bib_parts)
    # No global text rewriting here. A previous `{'` -> `{\\'` replace was meant to
    # escape an accent but only ever matched `{'}s` (ACL Anthology's export of an
    # apostrophe, valid BibTeX as-is) and rewrote it to `\\` -- a LaTeX line break.
    return bib_out, BibCategories(**cats), bibs_seen, under_review, non_papers


def _check_coverage(parsed, df, bibs_seen):
    """Warn about xlsx rows whose bib key is absent from orig.bib."""
    bib_keys_in_orig = {d["item_name"] for d in parsed}
    real_bib = df["Bib"].notna() & ~df["Bib"].str.lower().isin(("nan", "none", ""))
    df_with_bib = df[real_bib]
    if bibs_seen != len(df_with_bib):
        print(f"Warning: matched {bibs_seen} bib entries but {len(df_with_bib)} xlsx rows have a Bib key")
        unmatched = df_with_bib[~df_with_bib["Bib"].isin(bib_keys_in_orig)]
        if not unmatched.empty:
            print("  xlsx rows whose Bib key is absent from orig.bib:")
            for _, r in unmatched.iterrows():
                print(f"    {r['Bib']!r:40s}  {str(r['Name'])[:60]}")


def _bind_scholar_ids(result, df, store):
    """Record each matched paper's Scholar ID, so the next join is exact.

    This is what makes a fuzzy match a one-time event rather than a permanent
    condition: once a paper's `citation_for_view` ID is written down, its title
    can change on either side and the count still lands on the right row.

    Uses `result.source` rather than looking the row up by title, because for a
    fuzzy match the two titles differ by definition -- and those are precisely
    the ones worth binding.
    """
    name_to_key = {}
    for _, row in df.iterrows():
        name = str(row.get("Name") or "").strip()
        key = str(row.get("Bib") or "").strip()
        if name and key and key.lower() not in ("nan", "none"):
            name_to_key[name] = key

    bound = unbindable = 0
    for name, source_row in result.source.items():
        scholar_id = (source_row or {}).get("scholar_id")
        if not scholar_id:
            unbindable += 1
            continue
        key = name_to_key.get(name)
        if not key:
            # No bib key yet; step 3 assigns one and a later run binds this.
            continue
        before = (store.records.get(key) or {}).get("scholar_id")
        store.record(key, title=name, scholar_id=scholar_id)
        if not before:
            bound += 1

    if bound:
        print(f"Bound {bound} new Scholar ID(s) — those papers now match exactly")
    if unbindable:
        print(f"{unbindable} matched paper(s) have no Scholar ID in citations.csv. "
              f"Re-run fetch_citations.py to record them and make the join exact.")
    store.save()


def _report_duplicates(parsed, df):
    """Report same-paper-entered-twice, which no downstream join can resolve.

    A duplicate table row makes the citation join ambiguous by construction: two
    rows compete for one Scholar result. A duplicate BibTeX key makes the
    orig.bib lookup pick whichever entry parsed first. Both are reported here
    and collected by the worklist; neither is guessed at.
    """
    problems = []

    dup_titles = find_duplicate_titles(df["Name"].dropna())
    for norm, names in sorted(dup_titles.items()):
        problems.append(("duplicate-table-row", names[0], names))
        print(f"Warning: {len(names)} table rows are the same paper:")
        for n in names:
            print(f"    {n}")

    dup_keys = find_duplicate_keys(parsed)
    for key, n in sorted(dup_keys.items()):
        problems.append(("duplicate-bib-key", key, [f"{n} entries in orig.bib"]))
        print(f"Warning: BibTeX key {key!r} appears {n} times in orig.bib")

    bib_col = df["Bib"].dropna()
    dup_bib_cells = {}
    for value in bib_col:
        text = str(value).strip()
        if text and text.lower() not in ("nan", "none"):
            dup_bib_cells[text] = dup_bib_cells.get(text, 0) + 1
    for key, n in sorted(k for k in dup_bib_cells.items() if k[1] > 1):
        problems.append(("duplicate-bib-cell", key, [f"{n} table rows"]))
        print(f"Warning: Bib key {key!r} is used by {n} table rows")

    return problems


def main():
    with open(os.path.join(FILE_DIR, "orig.bib")) as f:
        bib_raw = f.read()

    parsed = parse_bibtex(bib_raw)
    df = read_df()
    _report_duplicates(parsed, df)
    store = IdentityStore.load()
    citation_rows = load_citations(os.path.join(FILE_DIR, "citations.csv"))
    # Sorted, not a set: with a duplicate table row only one can win, and which
    # one must not vary with PYTHONHASHSEED between runs.
    join = _build_name2cite(citation_rows,
                            sorted(set(df["Name"].dropna())),
                            store=store)
    name2cite = join.matched
    _bind_scholar_ids(join, df, store)

    bib_out, cats, bibs_seen, under_review, non_papers = _process_entries(parsed, df, name2cite)

    enhanced_path = os.path.join(FILE_DIR, "overleaf", "Wzmn.bib")
    with open(enhanced_path, "w") as f:
        f.write(bib_out)

    _check_coverage(parsed, df, bibs_seen)

    print(f"Skipped {under_review} under-review and {non_papers} non-papers")
    print(f"Bib exported to {os.path.abspath(enhanced_path)}")
    print("All venues: " + str([x for x in df["Venue"].apply(simplify_venue).unique()
                                  if x and "xiv" not in x and "review" not in x]))
    for field, keys in cats._asdict().items():
        print(f"% {_CATEGORY_LABELS[field]}:\n\\nocite{{{','.join(keys)}}}")

    return cats


if __name__ == "__main__":
    main()
