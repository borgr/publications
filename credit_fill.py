"""Give back the author list an entry was written without.

When the arXiv abstract page of a new paper cannot be fetched -- arXiv
rate-limiting a long run, or no network -- `fetch_arxiv_bib` falls back to
`_bare_arxiv_bib`, a stub with a title, a year and the arXiv ID, and no authors.
That is the right call for that run: a stub keeps the paper in the CV instead of
dropping it. But nothing came back for it. Part A re-checks arXiv entries only
for a *published* version, so a preprint that is still a preprint kept its stub
forever, and `lists_author` accepts an entry that credits nobody. The Sep 30 run
wrote two such stubs, and the CV printed two of its owner's own papers -- one of
them a preprint a grant proposal cites -- with no authors at all.

This module only adds an `author` field to an entry that has neither `author` nor
`editor`. It takes the list from the arXiv abstract page of the ID the entry
already carries; an entry with no arXiv ID is left for WORKLIST.md.
"""

import resolve_arxiv
from bib_edit import _get_arxiv_id
from bib_utils import extract_field, parse_bibtex
from link_fill import add_fields

CREDIT_FIELDS = ("author", "editor")

# Counted in resolve_attempts.json under their own namespace, like link_fill's.
ATTEMPT_PREFIX = "credits:"


def has_credits(content: str) -> bool:
    """True when the entry names its authors, or the editors of a volume."""
    return any(extract_field(content, f).strip() for f in CREDIT_FIELDS)


def entries_without_credits(bib_text: str) -> list[dict]:
    return [e for e in parse_bibtex(bib_text) if not has_credits(e["content"])]


def authors_from_arxiv(arxiv_id: str, key: str, title: str = "") -> str:
    """The author field arXiv's abstract page gives for this ID, or "" if none.

    `fetch_arxiv_bib` returns the same author-less stub when the page cannot be
    read, so an empty answer here means "try again next run", not "no authors".
    """
    bib = resolve_arxiv.fetch_arxiv_bib(arxiv_id, key, title)
    return extract_field(bib, "author").strip()


def fill_credits(bib_text: str, attempts: dict, lookup=None) -> tuple[str, int, list]:
    """Add the author list to every entry that credits nobody and has an arXiv ID.

    Returns the new text, how many entries gained authors, and the (title, key)
    pairs still crediting nobody.
    """
    lookup = lookup or authors_from_arxiv
    filled = 0
    still_missing = []
    for entry in entries_without_credits(bib_text):
        key = entry["item_name"]
        arxiv_id = _get_arxiv_id(entry)
        authors = ""
        if arxiv_id:
            before = resolve_arxiv.unanswered_lookups()
            authors = lookup(arxiv_id, key, entry["title"])
            if authors or resolve_arxiv.unanswered_lookups() == before:
                attempts[ATTEMPT_PREFIX + key] = attempts.get(ATTEMPT_PREFIX + key, 0) + 1
        if authors:
            bib_text = add_fields(bib_text, key, {"author": authors})
            filled += 1
            print(f"    [{key[:40]:<40}] → {authors[:60]}")
        else:
            still_missing.append((entry["title"][:70], key))
    return bib_text, filled, still_missing
