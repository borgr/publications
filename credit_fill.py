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
already carries.

An entry with no arXiv ID but a DOI is asked of the DOI instead. The case that
needed it is an edited volume: Scholar lists "Proceedings of the Second BabyLM
Workshop" under its editors, the entry arrives as a bare DOI stub, and arXiv has
nothing to say about a proceedings volume. Crossref's record of an ACL volume DOI
names nobody either, so an ACL Anthology DOI (10.18653/...) is read from the
Anthology's own BibTeX, which carries the `editor` list. Whatever the source
names -- `author` or `editor` -- is what the entry gains. An entry with neither
an arXiv ID nor a DOI is left for WORKLIST.md.
"""

import re

import resolve_arxiv
from bib_edit import _get_arxiv_id
from bib_utils import extract_field, parse_bibtex
from link_fill import add_fields
from resolve_arxiv import _bib_year, _dblp_title, titles_agree

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


# A paper's Anthology id ends in its number within the volume
# (2026.babylm-main.12); a volume's does not (2026.babylm-main), and the
# Anthology serves the volume's own entry as paper 0.
_ACL_DOI_PREFIX = "10.18653/"
_ACL_PAPER_RE = re.compile(r"\.\d+$")


def _acl_id_of(doi: str) -> str | None:
    """The ACL Anthology id whose BibTeX describes this DOI, or None."""
    if not doi.lower().startswith(_ACL_DOI_PREFIX):
        return None
    acl_id = doi[len(_ACL_DOI_PREFIX):]
    return acl_id if _ACL_PAPER_RE.search(acl_id) else acl_id + ".0"


def credits_from_doi(doi: str, key: str, title: str = "", year: int | None = None) -> dict:
    """{"author": ...} or {"editor": ...} as the DOI's publisher names them, or {}.

    Guarded by a title check, as every DOI lookup here is: a wrong DOI answers with
    a well-formed record of somebody else's work.
    """
    acl_id = _acl_id_of(doi)
    if acl_id:
        bib = resolve_arxiv.fetch_acl_bib(acl_id, key) or ""
        if bib and not titles_agree(title, _dblp_title(bib), year, _bib_year(bib)):
            bib = ""
    else:
        bib = resolve_arxiv.fetch_by_doi_crossref(doi, key, title, year) or ""
    for field in CREDIT_FIELDS:
        value = extract_field(bib, field).strip()
        if value:
            return {field: value}
    return {}


def fill_credits(bib_text: str, attempts: dict, lookup=None,
                 doi_lookup=None) -> tuple[str, int, list]:
    """Credit every entry that names nobody, from its arXiv ID or else its DOI.

    Returns the new text, how many entries gained credits, and the (title, key)
    pairs still crediting nobody.
    """
    lookup = lookup or authors_from_arxiv
    doi_lookup = doi_lookup or credits_from_doi
    filled = 0
    still_missing = []
    for entry in entries_without_credits(bib_text):
        key = entry["item_name"]
        arxiv_id = _get_arxiv_id(entry)
        doi = extract_field(entry["content"], "doi").strip()
        credits = {}
        if arxiv_id or doi:
            before = resolve_arxiv.unanswered_lookups()
            if arxiv_id:
                authors = lookup(arxiv_id, key, entry["title"])
                credits = {"author": authors} if authors else {}
            else:
                credits = doi_lookup(doi, key, entry["title"],
                                     _bib_year(entry["content"]))
            if credits or resolve_arxiv.unanswered_lookups() == before:
                attempts[ATTEMPT_PREFIX + key] = attempts.get(ATTEMPT_PREFIX + key, 0) + 1
        if credits:
            bib_text = add_fields(bib_text, key, credits)
            filled += 1
            (field, names), = credits.items()
            print(f"    [{key[:40]:<40}] → {field}: {names[:52]}")
        else:
            still_missing.append((entry["title"][:70], key))
    return bib_text, filled, still_missing
