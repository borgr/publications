"""Give every bibliography entry a link a reader can follow.

The CV makes each title a link, the way the ACL bibliography style does: the
title points at the DOI, else the arXiv abstract page, else the entry's URL, and
no identifier is printed. An entry with none of the three prints as a title that
goes nowhere -- and an evaluator who wants to read a preprint has no way to find
it. That is how a grant office came to ask for the arXiv link of a preprint the CV
listed without one.

Nineteen entries in orig.bib had no link at all, mostly recent preprints that
arrived from Scholar as bare `@article{..., journal={arXiv preprint ...}}` records
without the arXiv ID. Nothing looked for one, because every other lookup in the
pipeline is after a *published version*, and a preprint that is still a preprint
is, to those lookups, already done.

This module only adds identifiers. It never replaces an entry, never touches a
field the entry already has, and accepts an answer only when `titles_agree`
says the record is this paper -- the same guard the rest of step 3 relies on.
"""

import html
import re
from urllib.parse import quote

import resolve_arxiv
from bib_edit import _get_arxiv_id
from bib_utils import extract_field, parse_bibtex
from resolve_arxiv import _as_year, titles_agree

# The lookups are reached through the module, not imported by name, so that the
# test suite's guard against unstubbed network calls -- which patches them on
# resolve_arxiv -- covers this module too.

ARXIV_API = "https://export.arxiv.org/api/query"
# arXiv asks API clients for three seconds between requests. A run makes a handful
# of these at most, so honouring it costs nothing.
_ARXIV_API_SPACING_SECONDS = 3.0
_ARXIV_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_ARXIV_ID_RE = re.compile(r"<id>\s*https?://arxiv\.org/abs/([^<\s]+?)(?:v\d+)?\s*</id>")
_ARXIV_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_ARXIV_YEAR_RE = re.compile(r"<published>\s*(\d{4})")

LINK_FIELDS = ("doi", "eprint", "url")

# arXiv mints a DOI for every submission. It is a working link, but it is the
# preprint's, so a published DOI from the same record is preferred over it, and
# an arXiv ID is preferred over the arXiv DOI because the style prints the
# abstract page for it, which is the page a reader wants.
_ARXIV_DOI_PREFIX = "10.48550/arxiv."

# Attempts are counted in resolve_attempts.json under their own namespace, so a
# paper that has a published version but no link is not deprioritized for the
# published-version lookup, and the reverse.
ATTEMPT_PREFIX = "link:"


def has_link(content: str) -> bool:
    """True when the entry body carries a DOI, an arXiv ID or a URL."""
    return any(extract_field(content, f).strip() for f in LINK_FIELDS)


def entries_without_link(bib_text: str) -> list[dict]:
    return [e for e in parse_bibtex(bib_text) if not has_link(e["content"])]


def _clean_doi(doi: str) -> str:
    doi = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    return doi


def _arxiv_id_from_doi(doi: str) -> str:
    """'10.48550/arXiv.2504.11442' -> '2504.11442', else ''."""
    return doi[len(_ARXIV_DOI_PREFIX):] if doi.lower().startswith(_ARXIV_DOI_PREFIX) else ""


def _fields_for(doi: str = "", arxiv_id: str = "") -> dict:
    """The link fields to add, preferring a published DOI, then arXiv, then any DOI."""
    doi = _clean_doi(doi)
    arxiv_id = (arxiv_id or "").strip() or _arxiv_id_from_doi(doi)
    if doi and not _arxiv_id_from_doi(doi):
        return {"doi": doi}
    if arxiv_id:
        return {"eprint": arxiv_id, "archiveprefix": "arXiv"}
    if doi:
        return {"doi": doi}
    return {}


def link_fields_from_s2(record: dict | None) -> dict:
    ids = (record or {}).get("externalIds") or {}
    return _fields_for(ids.get("DOI") or "", ids.get("ArXiv") or "")


def link_fields_from_openalex(work: dict | None) -> dict:
    return _fields_for((work or {}).get("doi") or "")


def search_arxiv_by_title(title: str, year: str = "") -> str:
    """The arXiv ID of the preprint with this title, or "" if arXiv has none.

    The entries left without a link are almost all preprints, and arXiv is the one
    source that is sure to index a preprint, needs no API key, and is not the
    Semantic Scholar endpoint that rate-limits a keyless run into a long pause.
    """
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"[{}]", "", title))
    if not words:
        return ""
    query = "ti:%22" + quote(" ".join(words)) + "%22"
    resolve_arxiv.time.sleep(_ARXIV_API_SPACING_SECONDS)
    body = resolve_arxiv._curl_get(f"{ARXIV_API}?search_query={query}&max_results=5")
    if not body:
        return ""
    query_year = _as_year(year)
    for entry in _ARXIV_ENTRY_RE.findall(body):
        id_match = _ARXIV_ID_RE.search(entry)
        title_match = _ARXIV_TITLE_RE.search(entry)
        if not id_match or not title_match:
            continue
        found_title = html.unescape(" ".join(title_match.group(1).split()))
        year_match = _ARXIV_YEAR_RE.search(entry)
        found_year = int(year_match.group(1)) if year_match else None
        if titles_agree(title, found_title, query_year, found_year):
            return id_match.group(1)
    return ""


def find_link(title: str, year: str = "") -> dict:
    """Link fields for the paper with this title, or {} if no source has it.

    Semantic Scholar first, because its externalIds carry the arXiv ID as well as
    the DOI; OpenAlex second, which knows DOIs S2 lacks; arXiv last, for the
    preprint neither has caught up with. All three answer a query with their best
    guesses, and all three reject a guess whose title or year does not agree with
    the one asked for.
    """
    record = resolve_arxiv.query_s2_by_title(title, year)
    fields = link_fields_from_s2(record)
    if fields:
        return fields
    work = resolve_arxiv.search_openalex(title, _as_year(year))
    fields = link_fields_from_openalex(work)
    if fields:
        return fields
    return _fields_for(arxiv_id=search_arxiv_by_title(title, year))


def add_fields(bib_text: str, key: str, fields: dict) -> str:
    """Return bib_text with `fields` appended to entry `key`, leaving the rest as is.

    Fields the entry already has are left alone, so a hand-set value always wins.
    """
    for entry in parse_bibtex(bib_text):
        if entry["item_name"] != key:
            continue
        missing = {f: v for f, v in fields.items()
                   if not extract_field(entry["content"], f).strip()}
        if not missing:
            return bib_text
        old = entry["beg"] + entry["rest"]
        body = entry["content"].rstrip()
        if not body.endswith(","):
            body += ","
        added = "".join(f"\n  {f} = {{{v}}}," for f, v in missing.items())
        new = entry["beg"] + body + added + "\n}"
        return bib_text.replace(old, new, 1)
    return bib_text


def fill_links(bib_text: str, attempts: dict, lookup=None) -> tuple[str, int, list]:
    """Add a link to every entry that has none.

    Returns the new text, how many entries gained a link, and the
    (title, key) pairs still without one. Only a lookup that completed counts as
    an attempt, as in the rest of step 3: a network failure is not the paper's
    fault and must not push it down the queue.
    """
    lookup = lookup or find_link
    filled = 0
    still_missing = []
    for entry in entries_without_link(bib_text):
        key = entry["item_name"]
        # Scholar's export often names the arXiv ID inside the journal field
        # ("arXiv preprint arXiv:2504.11442") without an eprint field the style can
        # link. That needs no lookup at all.
        known_id = _get_arxiv_id(entry)
        if known_id:
            fields = _fields_for(arxiv_id=known_id)
        else:
            before = resolve_arxiv.unanswered_lookups()
            fields = lookup(entry["title"], extract_field(entry["content"], "year"))
            if fields or resolve_arxiv.unanswered_lookups() == before:
                attempts[ATTEMPT_PREFIX + key] = attempts.get(ATTEMPT_PREFIX + key, 0) + 1
        if fields:
            bib_text = add_fields(bib_text, key, fields)
            filled += 1
            print(f"    [{key[:40]:<40}] → {', '.join(f'{k}={v}' for k, v in fields.items() if k != 'archiveprefix')}")
        else:
            still_missing.append((entry["title"][:70], key))
    return bib_text, filled, still_missing
