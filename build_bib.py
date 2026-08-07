import os
import re
from datetime import datetime
from typing import NamedTuple

import pandas as pd

from bib_utils import (
    choose_published,
    find_duplicate_keys,
    find_field_span,
    normalize_text,
    parse_bibtex,
    publication_rank,
    read_df,
)
from citations_io import read_citation_rows
from identity import (
    IdentityStore,
    duplicate_groups_by_identifier,
    find_duplicate_titles,
    join_citations,
)
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
NON_RANKED = _VENUES.non_ranked

# The truncation fallback's delimiters: a four-digit year, or a structural
# character. It used to split on the literal digit `2`, which happens to work for
# years in this century but mangles a venue whose name contains one --
# "K2 Workshop" became `k`, "H2O Symposium" became `h`. Matching a four-digit run
# instead keeps "CogSci2024" -> cogsci and "ACL 2024" -> acl while leaving those
# alone.
_VENUE_SPLIT_RE = re.compile(r'\d{4}|[\-*^(]')

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
    """Strip a previously-emitted `pretitle={...}` field.

    Brace-aware, so a tag value that itself contains braces is still removed;
    the old `[^}]*}` stopped at the first inner brace and left the field behind.
    """
    span = find_field_span(s, "pretitle")
    if span is None:
        return s
    start, end, _delim = span
    field_start = s.rfind("pretitle", 0, start)
    if field_start == -1:
        return s
    # Take the leading whitespace and the trailing delimiter/comma/newline with it.
    while field_start > 0 and s[field_start - 1] in " \t\n":
        field_start -= 1
    tail = end + 1
    while tail < len(s) and s[tail] in ",\n \t":
        tail += 1
    return s[:field_start] + "\n    " + s[tail:] if s[:field_start] else s[tail:]


_YEAR_IN_BOOKTITLE_RE = re.compile(r'\d{4}')


def shorten_booktitle(s):
    """Trim a booktitle after its year, dropping the city/pages tail.

    Edits the value inside whatever delimiter the field uses. The previous regex
    assumed a closing `}` and, on a quoted booktitle -- which is how the ACL
    Anthology exports them -- replaced the closing quote with a brace, producing
    BibTeX that does not parse. Not reachable in the current data, but the
    parser now accepts quoted fields, so the shape is one paste away.
    """
    span = find_field_span(s, "booktitle")
    if span is None:
        return s
    start, end, delim = span
    # Braced fields only. This is deliberately the pre-existing behaviour, which
    # differed by delimiter purely by accident: the old regex needed a closing `}`
    # and so never matched a quoted field. Keeping that split is the right call
    # rather than a coincidence -- DBLP and OpenReview brace their booktitles and
    # append a verbose tail worth trimming ("NeurIPS 2024 Competition Track"),
    # while the ACL Anthology quotes them and its full names are worth keeping
    # ("Proceedings of the 2024 Conference on EMNLP", where the year sits
    # mid-name and cutting there would destroy it).
    #
    # The bug being fixed is that the old regex, when it *did* match a quoted
    # field, replaced its closing quote with a brace and produced BibTeX that
    # does not parse. Going through the field's real extent cannot do that.
    if delim != '{':
        return s
    value = s[start:end]
    matches = list(_YEAR_IN_BOOKTITLE_RE.finditer(value))
    if not matches:
        return s
    cut = matches[0].end()
    if cut >= len(value.rstrip()):
        return s
    return s[:start] + value[:cut] + s[end:]


def simplify_venue(name):
    """Reduce a raw venue string to a venues.yaml key.

    Two strategies, in order. `match_raw` handles the full official names that
    Scholar reports ("Proceedings of the 26th Conference on Computational Natural
    Language ..." -> conll); the truncation heuristic handles the short forms a
    human types ("ACL 2024" -> acl). Whatever comes out of the fallback may not
    be a known key, and the worklist reports those rather than hiding them.
    """
    return venue_resolution(name)[0]


def venue_resolution(name):
    """Return (venue_key, how) where `how` is "matched", "truncated" or "".

    The truncation fallback splits on the first digit, dash, paren, caret or
    asterisk, which can cut mid-word: "Nature-inspired Computing" truncates to
    "nature" and would be filed as a Nature paper. Keeping the fallback (it is
    what makes "EMNLP-Findings" work) but reporting which venues relied on it
    means such a misfile is visible in WORKLIST.md instead of silent -- add a
    `match:` phrase in venues.yaml to resolve one explicitly.
    """
    if pd.isna(name) or not name:
        return "", ""
    matched = _VENUES.match_raw(name)
    if matched:
        return matched, "matched"
    s = str(name).lower()
    return _VENUE_SPLIT_RE.split(s, maxsplit=1)[0].strip(), "truncated"


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
    if result.aggregated:
        print("Summed across multiple Scholar records for the same paper "
              "(merging them in your Scholar profile makes this exact):")
        for name, cands, total in result.aggregated:
            print(f"  {total} total — {name[:60]}")
            for title, tier, score in cands:
                print(f"{'':>9}<- [{tier} {score:.0%}] {title[:60]}")
    if result.ambiguous:
        print("AMBIGUOUS: Scholar records that are NOT the same paper matched one "
              "table row — check for a duplicate or mistyped row:")
        for name, cands, _total in result.ambiguous:
            print(f"  table: {name[:66]}")
            for title, tier, score in cands:
                print(f"{'':>9}<- [{tier} {score:.0%}] {title[:60]}")
    if result.too_close:
        print("Scholar records that matched two table rows equally well "
              "(not attributed to either):")
        for title, _value, score in result.too_close:
            print(f"  {score:.0%}  {title[:66]}")
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
    if venue_simple in NON_RANKED:
        # A real outlet with no ranking (a blog post): non-reviewed, and
        # deliberately not a "cannot categorise" warning.
        return "drafts"
    return None


def _process_entries(parsed, df, name2cite, suppressed=()):
    """Build wzmn.bib text and categorise each paper.

    Returns (bib_out, BibCategories, bibs_seen, arxiv_only_count, non_paper_count).
    """
    bib_parts = []
    cats = {field: [] for field in BibCategories._fields}
    bibs_seen = arxiv_only = non_paper_rows = 0

    for dic in parsed:
        if dic["item_name"] in suppressed:
            # A duplicate of a paper already emitted from its published entry.
            continue
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
            non_paper_rows += 1
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
        # Each injected field is prepended with its own leading newline, so they
        # separate from each other but the last one lands on the same line as the
        # entry's first real field: `pretitle={},author = {...}`. Valid BibTeX, but
        # it defeats every line-anchored `^\s*author\s*=` in a reader's grep -- one
        # audit of this file reported 49 entries as having no author on that basis.
        if not rest.startswith("\n"):
            rest = "\n    " + rest.lstrip()
        rest = "\n    pretitle={" + tags + "}," + rest
        rest = "\n    citations={" + cite_count + "}," + rest

        if not is_arxiv and not is_workshop:
            venue_info = _VENUES.description(venue_simple)
            if venue_info:
                rest = "\n    venueinf={" + venue_info + "}," + rest
            elif venue_simple and not _VENUES.known(venue_simple):
                # Only an *unknown* venue is a problem. A known one with no
                # description is deliberate: `kind: other` (a blog) has no
                # ranking to state, and warning about it made a configured venue
                # look unconfigured.
                print(f"Warning: unknown venue {venue_raw!r} (key: {venue_simple!r}) — venueinf omitted")

        category = _categorize(venue_simple, is_arxiv, is_review, is_workshop)
        if category is None:
            print(f"Warning: cannot categorize venue {venue_raw!r} for {row_bib!r}, adding to drafts")
            category = "drafts"
        cats[category].append(row_bib)
        if is_arxiv:
            arxiv_only += 1

        bib_parts.append(beg + rest)

    bib_out = "".join(p + "\n\n" for p in bib_parts)
    # No global text rewriting here. A previous `{'` -> `{\\'` replace was meant to
    # escape an accent but only ever matched `{'}s` (ACL Anthology's export of an
    # apostrophe, valid BibTeX as-is) and rewrote it to `\\` -- a LaTeX line break.
    return bib_out, BibCategories(**cats), bibs_seen, arxiv_only, non_paper_rows


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


def resolve_duplicate_rows(parsed, df):
    """Decide which entry to emit when two table rows are the same paper.

    Returns (suppressed_keys, notes). The version of record wins, by
    `publication_rank` -- so an ACL @inproceedings beats the same paper's arXiv
    @misc. Without this both rows are emitted and the CV lists the paper twice,
    which is what it was doing.

    This resolves the *output*; `scripts/dedupe.py` fixes the table itself. Both
    use the same rule, so they cannot disagree.
    """
    by_key = {e["item_name"]: e for e in parsed}
    suppressed, notes = set(), []

    # Papers sharing an identifier. A stronger signal than a title, and it finds
    # retitled duplicates that title comparison cannot -- two such papers were
    # being emitted into the CV twice.
    rows_with_key = {str(v).strip() for v in df["Bib"].dropna()}
    for keys in duplicate_groups_by_identifier(IdentityStore.load(), rows_with_key):
        entries = [by_key[k] for k in keys if k in by_key]
        if len(entries) < 2:
            continue
        winner, losers = choose_published(entries)
        for loser in losers:
            suppressed.add(loser["item_name"])
        notes.append((winner["item_name"], [other["item_name"] for other in losers],
                      publication_rank(winner)))

    for names in find_duplicate_titles(df["Name"].dropna()).values():
        entries, rows_without_entry = [], []
        for name in names:
            cell = df[df["Name"] == name]["Bib"]
            key = str(cell.iloc[0]).strip() if len(cell) else ""
            if key and key in by_key:
                entries.append(by_key[key])
            else:
                rows_without_entry.append(name)
        if len(entries) < 2:
            continue
        winner, losers = choose_published(entries)
        for loser in losers:
            suppressed.add(loser["item_name"])
        notes.append((winner["item_name"], [other["item_name"] for other in losers],
                      publication_rank(winner)))
    return suppressed, notes


def _report_duplicates(parsed, df):
    """Report same-paper-entered-twice, which no downstream join can resolve.

    A duplicate table row makes the citation join ambiguous by construction: two
    rows compete for one Scholar result. A duplicate BibTeX key makes the
    orig.bib lookup pick whichever entry parsed first. Both are reported here
    and collected by the worklist; neither is guessed at.
    """
    problems = []

    dup_titles = find_duplicate_titles(df["Name"].dropna())
    for _norm, names in sorted(dup_titles.items()):
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

    suppressed, dedupe_notes = resolve_duplicate_rows(parsed, df)
    for winner, losers, rank in dedupe_notes:
        print(f"Duplicate paper: emitting {winner!r} (publication rank {rank}) and "
              f"omitting {losers} from the CV. Run scripts/dedupe.py to fix the table.")

    bib_out, cats, bibs_seen, arxiv_only, non_paper_rows = _process_entries(
        parsed, df, name2cite, suppressed=suppressed)

    enhanced_path = os.path.join(FILE_DIR, "overleaf", "Wzmn.bib")
    with open(enhanced_path, "w") as f:
        f.write(bib_out)

    _check_coverage(parsed, df, bibs_seen)

    # Not "skipped", which is what this said for years: both counts are emitted.
    # The arXiv ones go in the ArXiv Articles section and the Paper=0 rows are
    # written without venue enrichment, so a reader chasing a missing paper was
    # being told it had been left out when it was in the file all along.
    print(f"Emitted {arxiv_only} preprint(s) with no published version and "
          f"{non_paper_rows} row(s) marked as not a paper")
    print(f"Bib exported to {os.path.abspath(enhanced_path)}")
    print("All venues: " + str([x for x in df["Venue"].apply(simplify_venue).unique()
                                  if x and "xiv" not in x and "review" not in x]))
    for field, keys in cats._asdict().items():
        print(f"% {_CATEGORY_LABELS[field]}:\n\\nocite{{{','.join(keys)}}}")

    return cats


if __name__ == "__main__":
    main()
