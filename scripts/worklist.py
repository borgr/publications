#!/usr/bin/env python3
"""Generate WORKLIST.md: everything the pipeline cannot decide on its own.

Run standalone -- it re-reads the data files and needs no network, so the
worklist can be regenerated at any time:

    python scripts/worklist.py
    python scripts/worklist.py --check    # exit 1 if anything is open (for CI)

Why this exists: the pipeline used to print its open questions to stdout and
then exit, so anything not read in the moment was lost. Unresolved bib lookups,
fuzzy citation matches and unknown venues all silently accumulated. Writing them
to a committed file means they are browsable on GitHub and survive the run.

Only open items appear. A section that is absent is done.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_bib  # noqa: E402
from bib_utils import find_duplicate_keys, parse_bibtex, read_df  # noqa: E402
from citations_io import read_citation_rows  # noqa: E402
from identity import (MATCH_EXACT_ID, IdentityStore, find_duplicate_titles,  # noqa: E402
                      join_citations)
from resolve_arxiv import get_missing_bib_entries, load_attempts  # noqa: E402
from venues import Venues  # noqa: E402

WORKLIST_PATH = os.path.join(ROOT, "WORKLIST.md")
BIB_PATH = os.path.join(ROOT, "orig.bib")
CITATIONS_CSV = os.path.join(ROOT, "citations.csv")


class Section:
    def __init__(self, title, blurb, lines):
        self.title = title
        self.blurb = blurb
        self.lines = lines


def gather():
    """Collect open items. Returns (sections, total_count)."""
    with open(BIB_PATH) as f:
        bib_text = f.read()
    parsed = parse_bibtex(bib_text)
    df = read_df()
    names = list(df["Name"].dropna())
    citations = read_citation_rows(CITATIONS_CSV)
    store = IdentityStore.load()
    venues = Venues.load()
    attempts = load_attempts()

    sections = []

    # ── duplicates: these break the citation join by construction ────────────
    dup_rows = find_duplicate_titles(names)
    if dup_rows:
        lines = []
        for group in dup_rows.values():
            keys = []
            for name in group:
                cell = df[df["Name"] == name]["Bib"]
                key = str(cell.iloc[0]) if len(cell) else "?"
                keys.append(f"`{key}`")
            lines.append(f"- Same paper on {len(group)} rows ({', '.join(keys)}):")
            for name in group:
                lines.append(f"  - {name}")
        sections.append(Section(
            f"Duplicate rows in the publications table ({len(dup_rows)})",
            "Two rows for one paper means both compete for the same citation "
            "count, and both are emitted into the CV. Delete the row whose "
            "BibTeX key you do not want to keep.",
            lines))

    dup_keys = find_duplicate_keys(parsed)
    if dup_keys:
        sections.append(Section(
            f"Duplicate BibTeX keys in orig.bib ({len(dup_keys)})",
            "Lookups resolve to whichever entry parsed first.",
            [f"- `{k}` appears {n} times" for k, n in sorted(dup_keys.items())]))

    # ── papers with no bibliography entry ────────────────────────────────────
    missing = get_missing_bib_entries(bib_text, df=df)
    if missing:
        lines = []
        for entry in sorted(missing, key=lambda e: -attempts.get(e["item_name"], 0)):
            tried = attempts.get(entry["item_name"], 0)
            suffix = f" — {tried} failed lookup(s) so far" if tried else ""
            lines.append(f"- `{entry['item_name']}` — {entry['title']}{suffix}")
        sections.append(Section(
            f"Papers with no BibTeX entry ({len(missing)})",
            "`update.py` retries these automatically each run. An entry with "
            "several failed lookups is unlikely to resolve itself: paste the "
            "BibTeX into orig.bib by hand, or use `clibib <doi-or-url>` "
            "(see README) for the ones DBLP and ACL do not index.",
            lines))

    # ── citation matches that a human should confirm once ────────────────────
    result = join_citations(citations, names, store=store)
    if result.needs_review:
        lines = []
        for name, incoming, tier, score in sorted(result.needs_review,
                                                  key=lambda r: r[3]):
            lines.append(f"- {score:.0%} ({tier}) — table: {name}")
            lines.append(f"  - Scholar: {incoming}")
        sections.append(Section(
            f"Citation counts matched by title, not by identifier "
            f"({len(result.needs_review)})",
            "These are matched and in use, but on title similarity rather than "
            "a stable Scholar ID. Confirm each is the same paper; the next "
            "`fetch_citations.py` run binds the ID and they stop appearing.",
            lines))

    if result.ambiguous:
        lines = []
        for name, candidates in result.ambiguous:
            lines.append(f"- table: {name}")
            for title, tier, score in candidates:
                lines.append(f"  - {score:.0%} ({tier}) {title}")
        sections.append(Section(
            f"Ambiguous citation matches ({len(result.ambiguous)})",
            "More than one Scholar record matched one table row, usually "
            "because Scholar holds the preprint and the published version "
            "separately. Merging them in your Scholar profile fixes it at the "
            "source and adds the counts together.",
            lines))

    if result.unmatched:
        lines = [f"- {value if value is not None else '—'} citations — {title}"
                 for title, value in sorted(result.unmatched,
                                            key=lambda r: -(r[1] or 0))]
        sections.append(Section(
            f"Scholar records with no row in the table ({len(result.unmatched)})",
            "Either a paper to add, or something Scholar wrongly attributes to "
            "you. Step 2 adds genuinely new papers automatically, so anything "
            "persisting here needs a decision.",
            lines))

    # ── venues that fall through to the draft section ────────────────────────
    # Two different problems, which want different fixes: a venue genuinely
    # absent from venues.yaml, versus a Venue cell still holding the raw string
    # Scholar reported. Step 2 writes Scholar's venue text verbatim when it adds
    # a row, and that text never matches a key, so every auto-added paper needs
    # its Venue cell tidied once.
    def _looks_unparsed(key):
        return (len(key) > 15 or "," in key or "…" in key
                or any(ch.isdigit() for ch in key))

    unknown, unparsed = {}, {}
    for _, row in df.iterrows():
        raw = row.get("Venue")
        if not isinstance(raw, str) or not raw.strip():
            continue
        low = raw.lower()
        if "xiv" in low or "review" in low or "patent" in low:
            continue
        # Workshop papers are categorised by their flag, not their venue, and
        # deliberately carry no venueinf line.
        if row.get("Workshop-paper") == 1:
            continue
        key = build_bib.simplify_venue(raw)
        if not key or venues.known(key):
            continue
        bucket = unparsed if _looks_unparsed(key) else unknown
        bucket.setdefault(key, set()).add((raw.strip(), str(row.get("Name") or "")))

    if unparsed:
        lines = []
        for key, entries in sorted(unparsed.items()):
            raw, name = sorted(entries)[0]
            lines.append(f"- `{raw[:80]}`")
            lines.append(f"  - on: {name[:90]}")
        sections.append(Section(
            f"Venue cells still holding a raw Scholar string ({len(unparsed)})",
            "Step 2 copies Scholar's venue text verbatim when it adds a paper, "
            "and that text does not reduce to a venue key -- so the paper gets "
            "no `venueinf` line and is filed under ArXiv Articles. Replace each "
            "with the short venue name (`acl`, `tacl`, `neurips`, …).",
            lines))

    if unknown:
        lines = []
        for key, entries in sorted(unknown.items()):
            examples = "; ".join(sorted({raw for raw, _ in entries}))
            lines.append(f"- `{key}` — from: {examples[:130]}")
        sections.append(Section(
            f"Venues missing from venues.yaml ({len(unknown)})",
            "A venue with no entry gets no `venueinf` line and its papers are "
            "filed under ArXiv Articles. Add it to `venues.yaml` with a `kind` "
            "of journal or conference, then run "
            "`python scripts/refresh_venues.py` to fill in the ranking.",
            lines))

    # ── identifier conflicts ─────────────────────────────────────────────────
    conflicts = store.conflicts()
    if conflicts:
        sections.append(Section(
            f"Conflicting identifiers ({len(conflicts)})",
            "Two sources reported different values for the same field, which "
            "usually means two papers were conflated. Resolve by deleting the "
            "wrong record from `identity.json`.",
            [f"- `{key}` {field}: {', '.join(str(v) for v in values)}"
             for key, field, values in conflicts]))

    total = sum(len(s.lines) for s in sections)
    return sections, total, result


def render(sections, result):
    out = ["# What still needs you", "",
           "Regenerated by `python update.py` (or `python scripts/worklist.py`).",
           "**Open items only** — a section that is not here is done.", ""]

    exact = sum(1 for t in result.method.values() if t == MATCH_EXACT_ID)
    out += [f"Citation join: {len(result.matched)} papers matched, "
            f"{exact} by stable Scholar ID.", ""]

    if not sections:
        out += ["Nothing open. ✓", ""]
    for section in sections:
        out += [f"## {section.title}", "", section.blurb, ""]
        out += section.lines
        out += [""]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Generate WORKLIST.md")
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if any item is open (does not write)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    sections, total, result = gather()

    if args.check:
        print(f"{total} open item(s) across {len(sections)} section(s)")
        return 1 if total else 0

    text = render(sections, result)
    previous = ""
    if os.path.exists(WORKLIST_PATH):
        with open(WORKLIST_PATH) as f:
            previous = f.read()
    if text != previous:
        with open(WORKLIST_PATH, "w") as f:
            f.write(text)
    if not args.quiet:
        print(f"  {total} open item(s) in {len(sections)} section(s) → WORKLIST.md")
        for section in sections:
            print(f"    {section.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
