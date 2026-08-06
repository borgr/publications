#!/usr/bin/env python3
"""Resolve arXiv BibTeX entries to their published versions.

For each arXiv entry in orig.bib, and each publications-table row with no entry
yet, fetch the best available BibTeX. Sources in descending preference:

  1. DBLP title search   published @inproceedings/@article, gated on a 0.72
                         title-similarity check so DBLP returning a different
                         paper cannot overwrite a good entry
  2. S2 externalIds      the ACL Anthology or OpenReview record. Also the
                         identifier crosswalk -- one response carries ArXiv,
                         DOI, ACL, DBLP and CorpusId together
  3. DOI via clibib      optional, identifier-only. Covers DOI-bearing records
                         that DBLP and the ACL Anthology do not index
                         (journals, book chapters). Never used for title search
  4. OpenAlex            keyless, and indexes journals and preprints the earlier
                         sources miss. Gated on a 0.90 title-similarity check.
                         A preprint result is labelled as such so it cannot
                         replace a published entry
  5. DBLP CoRR entry     a clean arXiv entry, as a fallback
  6. arXiv abstract page last resort

Only sources in `_PUBLISHED_SOURCES` may *replace* an existing entry; an
arXiv-derived result never does, since that would downgrade a published paper
back to a preprint.

Usage:
    python resolve_arxiv.py [--bib orig.bib] [--output resolved.bib] [--skip-missing]

Dependencies: beautifulsoup4, curl. Optionally clibib, for the DOI path.
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FILE_DIR)

from bib_utils import (escape_field_value, is_wellformed_entry, normalize_text,
                       parse_bibtex, publication_rank, read_df)
from identity import harvest_ids_from_bibtex, harvest_ids_from_s2

DEFAULT_BIB    = os.path.join(FILE_DIR, "orig.bib")
DEFAULT_OUTPUT = os.path.join(FILE_DIR, "resolved.bib")
ATTEMPTS_PATH  = os.path.join(FILE_DIR, "resolve_attempts.json")

# Entries tried >= this many times are sorted to the end of each run.
# They still run (not skipped), but fresh entries get S2 quota first.
_DEPRIORITIZE_AFTER = 5

_CURL_FLAGS = [
    "--silent", "--compressed", "--max-time", "20",
    "-A", "resolve-arxiv-bib/1.0",
    "--cookie-jar", "/tmp/_resolve_arxiv_cookies.txt",
    "--cookie", "/tmp/_resolve_arxiv_cookies.txt",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _curl_get(url: str) -> str:
    result = subprocess.run(
        ["curl", *_CURL_FLAGS, url],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout if result.returncode == 0 else ""


# Semantic Scholar's unauthenticated quota is shared and small, so a 429 during a
# long run is expected rather than exceptional. This used to disable S2 for the
# remainder of the run on the second 429 -- and because the ACL Anthology and
# OpenReview are both reached *through* S2's externalIds, that lost all three. On
# a real run it triggered on the second paper, leaving the best sources
# unavailable for the other 87. Now it backs off and comes back.
_S2_COOLDOWN_SECONDS = 120
_s2_state = {"blocked_until": 0.0}


def s2_available() -> bool:
    """False while S2 is in a rate-limit cooldown."""
    if time.time() < _s2_state["blocked_until"]:
        return False
    if _s2_state["blocked_until"]:
        print("\n    S2 cooldown over, using it again", end="", flush=True)
        _s2_state["blocked_until"] = 0.0
    return True


def _http_get_json(url: str, retries: int = 2) -> dict | None:
    """GET JSON, pausing a rate-limited source rather than abandoning it."""
    if not s2_available():
        return None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "resolve-arxiv-bib/1.0"})
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            is_429 = "429" in str(exc) or getattr(exc, "code", None) == 429
            if is_429:
                if attempt == 0:
                    print("\n    S2 rate-limited, waiting 30s…", end="", flush=True)
                    time.sleep(30)
                    continue
                _s2_state["blocked_until"] = time.time() + _S2_COOLDOWN_SECONDS
                print(f"\n    S2 still rate-limited — pausing it for "
                      f"{_S2_COOLDOWN_SECONDS}s", end="", flush=True)
                return None
            if attempt == retries - 1:
                print(f"\n    HTTP error ({exc})", file=sys.stderr, end="")
                return None
            time.sleep(3)
    return None


# ── Attempt tracking ─────────────────────────────────────────────────────────

def load_attempts() -> dict:
    """Load {key: attempt_count} from disk; return empty dict if missing."""
    try:
        with open(ATTEMPTS_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_attempts(attempts: dict) -> None:
    """Persist attempt counts atomically."""
    tmp = ATTEMPTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(attempts, f, indent=2, sort_keys=True)
    os.replace(tmp, ATTEMPTS_PATH)


def sort_by_attempts(candidates: list, attempts: dict) -> list:
    """Return candidates sorted so least-tried entries come first.

    Entries with >= _DEPRIORITIZE_AFTER prior attempts are moved to the end
    so fresh/new entries consume S2 quota before repeatedly-failing ones.
    """
    def _key(e):
        n = attempts.get(e["item_name"], 0)
        # Two-level sort: deprioritized bucket (1) vs fresh (0), then count within bucket
        return (1 if n >= _DEPRIORITIZE_AFTER else 0, n)
    return sorted(candidates, key=_key)


# ── BibTeX utilities ──────────────────────────────────────────────────────────

def _is_arxiv(entry: dict) -> bool:
    etype = entry["type"].lower()
    c = entry["content"].lower()
    # Published venues are never arXiv preprints
    if etype == "inproceedings":
        return False
    if etype == "article":
        j = re.search(r'journal\s*=\s*\{([^}]+)\}', c)
        if j and not re.search(r'\b(arxiv|corr)\b', j.group(1)):
            return False
    return bool(
        re.search(r'\barchiveprefix\b', c)
        or re.search(r'\beprinttype\b', c)
        or re.search(r'\beprint\s*=', c)
        or re.search(r'journal\s*=\s*\{[^}]*(arxiv|corr)', c)
    )


def _get_arxiv_id(entry: dict) -> str | None:
    content = entry["content"]
    m = re.search(r'\beprint\s*=\s*[{"]([0-9]+\.[0-9]+)[}"]', content)
    if m:
        return m.group(1)
    m = re.search(r'[Aa]r[Xx]iv:([0-9]+\.[0-9]+)', content)
    if m:
        return m.group(1)
    m = re.search(r'arxiv\.org/abs/([0-9]+\.[0-9]+)', content, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _replace_key(bibtex: str, new_key: str) -> str:
    """Rewrite an entry's key. Uses a function replacement, never a template.

    `re.sub` parses its *replacement* for backreferences and escapes, so any
    interpolated text containing a backslash is a live grenade: a BibTeX entry
    with a LaTeX accent (`\\i`, `\\'e`) raises `re.error: bad escape`. A callable
    replacement is taken literally.
    """
    return re.sub(r'(@\w+\s*\{)\s*[^,\s]+\s*,',
                  lambda m: m.group(1) + new_key + ",", bibtex, count=1)


def _simplify_title(t: str) -> str:
    return re.sub(r'[\W_]+', '', t.lower())


def _dblp_title(bibtex: str) -> str:
    m = re.search(r'\btitle\s*=\s*[{"]([^}"]+)', bibtex, re.IGNORECASE)
    return m.group(1) if m else ""


def _is_corr(bibtex: str) -> bool:
    compact = bibtex.lower().replace(" ", "").replace("\n", "")
    return (
        "journal={corr}" in compact
        or "eprinttype={arxiv}" in compact
        or "archiveprefix={arxiv}" in compact
        or "arxivpreprint" in compact
    )


def _extract_openreview_id(text: str) -> str | None:
    m = re.search(r'openreview\.net/forum\?id=([A-Za-z0-9_-]+)', text)
    return m.group(1) if m else None


# ── DBLP ──────────────────────────────────────────────────────────────────────

def search_dblp(title: str) -> list[str]:
    """Search DBLP by title, return list of BibTeX strings (up to 5)."""
    url = f"https://dblp.org/search/publ/api?q={quote(title)}&format=bib&h=5"
    raw = _curl_get(url)
    if not raw or raw.strip().startswith("<"):
        return []
    entries = re.split(r'\n(?=@)', raw.strip())
    return [e.strip() for e in entries if e.strip().startswith("@")]


def pick_published(bibtex_list: list[str], query_title: str = "") -> tuple[str | None, str | None]:
    """Return (first_published, first_corr) from DBLP results.

    When query_title is given, published entries whose title similarity is below
    0.72 are skipped — this guards against DBLP returning a different paper.
    """
    published = corr = None
    for bib in bibtex_list:
        if _is_corr(bib):
            if corr is None:
                corr = bib
        else:
            if published is None:
                if query_title:
                    ratio = difflib.SequenceMatcher(
                        None,
                        _simplify_title(query_title),
                        _simplify_title(_dblp_title(bib)),
                    ).ratio()
                    if ratio < 0.72:
                        continue
                published = bib
    return published, corr


# ── Semantic Scholar ──────────────────────────────────────────────────────────

def query_s2_by_arxiv(arxiv_id: str) -> dict | None:
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
        f"?fields=externalIds,publicationVenue"
    )
    return _http_get_json(url)


def query_s2_by_title(title: str, year: str = "") -> dict | None:
    params = urlencode({
        "query": title,
        "fields": "externalIds,publicationVenue,year,title",
        "limit": "3",
    })
    data = _http_get_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}")
    if not data or not data.get("data"):
        return None
    for candidate in data["data"]:
        if year:
            try:
                if abs(int(candidate.get("year", 0)) - int(year)) > 1:
                    continue
            except (ValueError, TypeError):
                pass
        return candidate
    return data["data"][0] if data["data"] else None


# ── ACL Anthology ─────────────────────────────────────────────────────────────

def fetch_acl_bib(acl_id: str, original_key: str) -> str | None:
    bib = _curl_get(f"https://aclanthology.org/{acl_id}.bib")
    if not bib or not bib.strip().startswith("@"):
        return None
    return _replace_key(bib.strip(), original_key)


# ── OpenReview ────────────────────────────────────────────────────────────────

def fetch_openreview_bib(forum_id: str, original_key: str) -> str | None:
    data = _http_get_json(f"https://api2.openreview.net/notes/{forum_id}")
    if not data:
        return None
    content = data.get("content", {})

    def _val(field):
        v = content.get(field, {})
        return v.get("value", v) if isinstance(v, dict) else v

    title = _val("title") or ""
    authors = _val("authors") or []
    if isinstance(authors, list):
        authors = " and ".join(authors)
    venue = _val("venue") or _val("venueid") or ""
    year = str(data.get("cdate", ""))[:4] or str(_val("year") or "")
    if not title:
        return None
    return (
        f"@inproceedings{{{original_key},\n"
        f"  title = {{{escape_field_value(title)}}},\n"
        f"  author = {{{escape_field_value(authors)}}},\n"
        f"  booktitle = {{{escape_field_value(venue)}}},\n"
        f"  year = {{{escape_field_value(year)}}},\n"
        f"  url = {{https://openreview.net/forum?id={forum_id}}}\n"
        f"}}"
    )


# ── OpenAlex ──────────────────────────────────────────────────────────────────
#
# Keyless, and covers what DBLP and the ACL Anthology do not index -- Nature and
# other journals, and preprints neither has picked up. Measured on this repo's own
# unresolved tail, it resolves 6 of 10 that the earlier sources missed.
#
# Two queries, because they behave differently: `filter=title.search:` is a
# phrase filter over titles and finds exact papers the fuzzy `search` param
# misses; `search` is broader and catches the rest. Both are gated on a strict
# title-similarity check, because OpenAlex happily returns an unrelated paper
# rather than nothing -- "MuLER: Detailed and Scalable Reference-based
# Evaluation" came back as "Regional climate modeling on European scales".

OPENALEX_WORKS = "https://api.openalex.org/works"
_OPENALEX_SELECT = ("id,title,doi,type,publication_year,authorships,"
                    "primary_location,biblio")

# Below this title similarity an OpenAlex result is a different paper.
_OPENALEX_MIN_RATIO = 0.90


def _openalex_query(url):
    raw = _curl_get(url)
    if not raw:
        return []
    try:
        return (json.loads(raw) or {}).get("results") or []
    except (json.JSONDecodeError, AttributeError):
        return []


def search_openalex(title):
    """Return the OpenAlex work matching `title`, or None."""
    if len(normalize_text(title)) < 10:
        return None
    mailto = ""
    try:
        import config
        if getattr(config, "CONTACT_EMAIL", ""):
            mailto = "&mailto=" + quote(config.CONTACT_EMAIL)
    except Exception:
        pass

    urls = [
        f"{OPENALEX_WORKS}?filter=title.search:{quote(title)}"
        f"&per-page=3&select={_OPENALEX_SELECT}{mailto}",
        f"{OPENALEX_WORKS}?{urlencode({'search': title, 'per-page': '3'})}"
        f"&select={_OPENALEX_SELECT}{mailto}",
    ]
    for url in urls:
        for work in _openalex_query(url):
            ratio = difflib.SequenceMatcher(
                None, _simplify_title(title), _simplify_title(work.get("title") or "")
            ).ratio()
            if ratio >= _OPENALEX_MIN_RATIO:
                return work
        time.sleep(0.4)
    return None


def openalex_to_bibtex(work, original_key):
    """Render an OpenAlex work as BibTeX. Returns (bibtex, is_published)."""
    title = (work.get("title") or "").strip()
    if not title:
        return None, False

    authors = " and ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    )
    year = work.get("publication_year") or ""
    doi = str(work.get("doi") or "").replace("https://doi.org/", "")
    source = ((work.get("primary_location") or {}).get("source") or {})
    venue = (source.get("display_name") or "").strip()
    biblio = work.get("biblio") or {}
    work_type = (work.get("type") or "").lower()

    # An arXiv/preprint record is not a published version, and must not be
    # allowed to replace a better entry -- hence the flag rather than a guess.
    is_preprint = (work_type in ("preprint", "posted-content")
                   or "arxiv" in venue.lower()
                   or doi.startswith("10.48550/"))

    if is_preprint:
        entry_type, venue_field = "misc", None
    elif work_type in ("article", "review", "paratext") and venue:
        entry_type, venue_field = "article", ("journal", venue)
    elif venue:
        entry_type, venue_field = "inproceedings", ("booktitle", venue)
    else:
        entry_type, venue_field = "misc", None

    # Values come from JSON, so they are plain text and must be escaped before
    # going inside braces -- an unbalanced brace in a title is otherwise fatal.
    lines = [f"@{entry_type}{{{original_key},",
             f"  title = {{{escape_field_value(title)}}},"]
    if authors:
        lines.append(f"  author = {{{escape_field_value(authors)}}},")
    if venue_field:
        lines.append(f"  {venue_field[0]} = {{{escape_field_value(venue_field[1])}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    for field, key in (("volume", "volume"), ("issue", "number")):
        if biblio.get(field):
            lines.append(f"  {key} = {{{biblio[field]}}},")
    if biblio.get("first_page"):
        pages = str(biblio["first_page"])
        if biblio.get("last_page"):
            pages += f"--{biblio['last_page']}"
        lines.append(f"  pages = {{{pages}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    lines.append("}")
    return "\n".join(lines), not is_preprint


# ── clibib (optional) ─────────────────────────────────────────────────────────
#
# https://github.com/delip/clibib -- a client for a Zotero translation server,
# used here for ONE thing: turning a known DOI into BibTeX. That fills a real gap,
# since this module has no DOI resolver, and DOI-only records (journals, book
# chapters, anything outside DBLP and the ACL Anthology) are most of what is left
# unresolved.
#
# Deliberately never used for title search. Measured against this repo's own
# unresolved papers, clibib's title lookup returned a confidently wrong paper 2
# times in 5 -- "Reinforcement learning with large action spaces for neural
# machine translation" came back as an unrelated Springer proceedings volume, and
# "Every eval ever: Toward a common language for ai eval reporting" came back as
# "A Common Language for Reporting Earthquake Intensities". Neither raised. A
# silent wrong entry in a CV is worse than a missing one, and clibib's own README
# says to prefer identifiers. Its identifier paths are exact and fast.
#
# Optional: absent clibib, this degrades to the arXiv fallback as before.

_CLIBIB_STATE = {"checked": False, "fn": None}


def _clibib_fetch():
    """Return clibib's fetch_bibtex, or None when it is not installed."""
    if not _CLIBIB_STATE["checked"]:
        _CLIBIB_STATE["checked"] = True
        try:
            from clibib.api import fetch_bibtex
            _CLIBIB_STATE["fn"] = fetch_bibtex
        except Exception:
            _CLIBIB_STATE["fn"] = None
    return _CLIBIB_STATE["fn"]


def fetch_by_doi(doi: str, original_key: str) -> str | None:
    """Resolve a DOI to BibTeX via clibib. Returns None on any failure."""
    fetch = _clibib_fetch()
    if not fetch or not doi:
        return None
    try:
        bib = fetch(doi)
    except Exception:
        return None
    if not bib or not bib.strip().startswith("@"):
        return None
    return _replace_key(bib.strip(), original_key)


# ── arXiv fallback ────────────────────────────────────────────────────────────

def fetch_arxiv_bib(arxiv_id: str, original_key: str) -> str:
    """Parse arXiv abstract page for metadata and build a @misc entry."""
    from bs4 import BeautifulSoup

    html = _curl_get(f"https://arxiv.org/abs/{arxiv_id}")
    if not html:
        return _bare_arxiv_bib(arxiv_id, original_key)

    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1", class_="title")
    title = (title_tag.get_text(strip=True).removeprefix("Title:").strip()
             if title_tag else "")

    authors = [a.get_text(strip=True)
               for a in (soup.find("div", class_="authors") or soup.new_tag("x")).find_all("a")]
    authors_str = " and ".join(authors)

    year = ""
    hist = soup.find("div", class_="submission-history")
    if hist:
        m = re.search(r'\b(20\d{2})\b', hist.get_text())
        if m:
            year = m.group(1)

    if not title:
        return _bare_arxiv_bib(arxiv_id, original_key)

    return (
        f"@misc{{{original_key},\n"
        f"  title = {{{escape_field_value(title)}}},\n"
        f"  author = {{{escape_field_value(authors_str)}}},\n"
        f"  year = {{{escape_field_value(year)}}},\n"
        f"  eprint = {{{arxiv_id}}},\n"
        f"  archivePrefix = {{arXiv}},\n"
        f"  url = {{https://arxiv.org/abs/{arxiv_id}}}\n"
        f"}}"
    )


def _bare_arxiv_bib(arxiv_id: str, key: str) -> str:
    m = re.match(r'(\d{2})', arxiv_id)
    year = f"20{m.group(1)}" if m else ""
    return (
        f"@misc{{{key},\n"
        f"  year = {{{year}}},\n"
        f"  eprint = {{{arxiv_id}}},\n"
        f"  archivePrefix = {{arXiv}},\n"
        f"  url = {{https://arxiv.org/abs/{arxiv_id}}}\n"
        f"}}"
    )


# ── Core resolver ─────────────────────────────────────────────────────────────

def resolve(title: str, arxiv_id: str | None, original_key: str,
            existing_content: str = "", store=None) -> tuple[str, str]:
    """Return (bibtex_string, source_label).

    When `store` is an IdentityStore, every identifier seen along the way is
    recorded against `original_key`. Semantic Scholar's `externalIds` is the
    valuable one: it returns ArXiv, DOI, ACL, DBLP and CorpusId for one paper in
    a single response, which is the crosswalk for the case where the ACL record
    knows no arXiv ID and the arXiv record knows no ACL ID. Recording it means
    later runs match on an identifier instead of guessing from a title.
    """
    corr_bib = None

    def _remember(**ids):
        if store is not None:
            store.record(original_key, title=title or None, **ids)

    if arxiv_id:
        _remember(arxiv=arxiv_id)

    # A title search on a blank or placeholder title returns an unrelated paper,
    # and a match would then overwrite a good entry. Prefer the arXiv fallback.
    title = (title or "").strip()
    if len(normalize_text(title)) < 10:
        if arxiv_id:
            return fetch_arxiv_bib(arxiv_id, original_key), "arXiv (no usable title)"
        return "", "no usable title"

    # Step 1 — DBLP title search
    dblp_results = search_dblp(title)
    published_bib, corr_bib = pick_published(dblp_results, query_title=title)
    if published_bib:
        _remember(**harvest_ids_from_bibtex(published_bib))
        return _replace_key(published_bib, original_key), "DBLP"
    time.sleep(1.0)

    # Step 2 — S2 to get ACL / OpenReview IDs
    s2_data = None
    if arxiv_id:
        s2_data = query_s2_by_arxiv(arxiv_id)
    else:
        year_m = re.search(r'\b(20\d{2})\b', existing_content)
        s2_data = query_s2_by_title(title, year_m.group(1) if year_m else "")
    time.sleep(1.5)

    if s2_data:
        ext = s2_data.get("externalIds") or {}
        # The crosswalk: one response binds every identifier this paper has.
        _remember(**harvest_ids_from_s2(s2_data))

        # ACL Anthology
        acl_id = ext.get("ACL")
        if acl_id:
            bib = fetch_acl_bib(acl_id, original_key)
            if bib:
                return bib, f"ACL Anthology"

        # OpenReview via S2 publicationVenue URL
        pub_venue = s2_data.get("publicationVenue") or {}
        or_id = _extract_openreview_id(pub_venue.get("url", "") or "")
        if or_id:
            bib = fetch_openreview_bib(or_id, original_key)
            if bib:
                return bib, "OpenReview"

    # OpenReview URL already in the existing bib entry
    or_id = _extract_openreview_id(existing_content)
    if or_id:
        bib = fetch_openreview_bib(or_id, original_key)
        if bib:
            return bib, "OpenReview"

    # Step 2b — a DOI we already know about, resolved through clibib. Only ever
    # by identifier, never by title. The DOI may have come from S2's externalIds
    # above, from the existing bib entry, or from a previous run's harvest.
    known_doi = ""
    if s2_data:
        known_doi = (s2_data.get("externalIds") or {}).get("DOI") or ""
    if not known_doi:
        known_doi = harvest_ids_from_bibtex(existing_content).get("doi", "")
    if not known_doi and store is not None:
        known_doi = (store.records.get(original_key) or {}).get("doi") or ""
    # An arXiv DOI (10.48550/...) only ever resolves back to the preprint, so
    # asking is a wasted request that then gets rejected by the rank guard -- it
    # did so 10 times on a real run.
    if known_doi and not known_doi.lower().startswith("10.48550/"):
        bib = fetch_by_doi(known_doi, original_key)
        if bib:
            _remember(doi=known_doi)
            return bib, "DOI (clibib)"

    # Step 2c — OpenAlex, which indexes journals and book chapters that DBLP and
    # the ACL Anthology do not. Its result may itself be a preprint, so the label
    # distinguishes the two: only the published one is allowed to replace an
    # existing entry.
    work = search_openalex(title)
    if work:
        bib, is_published = openalex_to_bibtex(work, original_key)
        if bib:
            ids = {}
            doi = str(work.get("doi") or "").replace("https://doi.org/", "")
            if doi and not doi.startswith("10.48550/"):
                ids["doi"] = doi
            _remember(**ids)
            return bib, ("OpenAlex" if is_published else "OpenAlex (preprint)")

    # Step 3 — arXiv fallback
    if corr_bib:
        return _replace_key(corr_bib, original_key), "arXiv (DBLP/CoRR)"
    if arxiv_id:
        return fetch_arxiv_bib(arxiv_id, original_key), "arXiv (export API)"

    return "", "not found"


# ── In-place bib update ───────────────────────────────────────────────────────

# Sources trusted enough to *replace* an existing orig.bib entry. An arXiv-derived
# result never replaces anything -- it would downgrade a published entry back to a
# preprint. "DOI (clibib)" qualifies because a DOI is an exact identifier.
_PUBLISHED_SOURCES = {"DBLP", "ACL Anthology", "OpenReview", "DOI (clibib)",
                      "OpenAlex"}


def gen_key(authors: str, year: str, title: str) -> str:
    """Generate a short bib key from author/year/title, e.g. 'yadav2023ties'."""
    last = re.sub(r'[^a-z]', '', authors.split(",")[0].strip().split()[-1].lower())
    skip = {"a", "an", "the", "of", "in", "on", "for", "with", "from", "to", "and", "is", "are"}
    word = next(
        (w.lower() for w in re.split(r'\W+', title) if w.lower() not in skip and len(w) > 2),
        "paper",
    )
    word = re.sub(r'[^a-z0-9]', '', word)
    return f"{last}{year}{word}"


def update_bib_inplace(
    bib_text: str,
    updates: list,   # list of (key, new_bibtex, source)
    new_entries: list,  # list of (key, new_bibtex) to append
) -> tuple:
    """Apply resolved BibTeX in-place; return (new_bib_text, n_replaced, n_appended)."""
    n_replaced = 0
    existing_by_key = {e["item_name"]: e for e in parse_bibtex(bib_text)}
    for key, new_bib, source in updates:
        if source not in _PUBLISHED_SOURCES:
            continue

        # Refuse anything that does not parse back to exactly this one entry.
        # Without it, a title containing an unbalanced brace is written verbatim,
        # fails to parse, and takes the remainder of the file with it.
        if not is_wellformed_entry(new_bib, expected_key=key):
            print(f"  [rejected] {key}: {source} returned BibTeX that does not "
                  f"parse back cleanly", file=sys.stderr)
            continue

        # Never trade a more-published entry for a less-published one. The source
        # label says where the replacement came from, not how good it is: DBLP
        # can return a workshop @misc for a paper whose existing entry is the
        # @inproceedings version of record.
        old = existing_by_key.get(key)
        if old is not None:
            candidates = parse_bibtex(new_bib)
            if candidates:
                new_rank = publication_rank(candidates[0])
                old_rank = publication_rank(old)
                if new_rank < old_rank:
                    print(f"  [keep existing] {key}: {source} result ranks lower "
                          f"({new_rank} < {old_rank})", file=sys.stderr)
                    continue

        pattern = re.compile(
            r'@\w+\s*\{' + re.escape(key) + r',.*?\r?\n\}',
            re.DOTALL,
        )
        # A callable replacement, so the new entry is inserted literally. Passing
        # the string directly made re parse it as a template, and a real run died
        # with `re.error: bad escape \i` on a LaTeX accent -- after every lookup
        # had been made, so the whole run's work was lost.
        replacement = new_bib.rstrip('\n')
        new_text, count = pattern.subn(lambda _m: replacement, bib_text, count=1)
        if count:
            bib_text = new_text
            n_replaced += 1
    n_appended = 0
    for _key, new_bib in new_entries:
        if re.search(r'@\w+\s*\{' + re.escape(_key) + r'\s*,', bib_text):
            print(f"  [skip duplicate] {_key} already in bib", file=sys.stderr)
            continue
        if not is_wellformed_entry(new_bib, expected_key=_key):
            print(f"  [rejected] {_key}: generated BibTeX does not parse back "
                  f"cleanly, not appended", file=sys.stderr)
            continue
        bib_text = bib_text.rstrip('\n') + '\n\n' + new_bib + '\n'
        n_appended += 1
    return bib_text, n_replaced, n_appended


# ── Candidate discovery ───────────────────────────────────────────────────────

def get_arxiv_entries(bib_text: str) -> list[dict]:
    return [e for e in parse_bibtex(bib_text) if _is_arxiv(e)]


def placeholder_key(year: str, title: str) -> str:
    """Key for a paper with no known first author, so gen_key cannot apply."""
    return f"unknown{year}{normalize_text(title)[:10]}"


def get_missing_bib_entries(bib_text: str, df=None) -> list[dict]:
    """Publications-table rows that have no usable entry in orig.bib.

    A row is missing when its Bib cell is empty, or names a key that orig.bib
    does not contain. The returned `item_name` is the key the entry will be
    filed under: an existing hand-assigned key is always preserved, otherwise
    one is generated from author/year/title.

    This is the single implementation. `update.py` previously carried a second,
    near-identical copy that generated keys differently -- so the same row could
    be filed under two different keys depending on which caller ran.
    """
    if df is None:
        try:
            df = read_df()
        except Exception as exc:
            print(f"Warning: could not read the publications table: {exc}", file=sys.stderr)
            return []
    existing_keys = {e["item_name"] for e in parse_bibtex(bib_text)}
    missing = []
    for _, row in df.iterrows():
        name = str(row.get("Name", "") or "").strip()
        if not name:
            continue
        bib_key = str(row.get("Bib", "") or "").strip()
        if bib_key.lower() in ("nan", "none"):
            bib_key = ""
        if bib_key and bib_key in existing_keys:
            continue

        authors = str(row.get("Authors", "") or "").strip()
        if authors.lower() == "nan":
            authors = ""
        try:
            year = str(int(row.get("year", 0) or 0))
        except (TypeError, ValueError):
            year = str(row.get("year", "") or "").strip()

        if bib_key:
            key = bib_key           # set by hand but not yet resolved; keep it
        elif authors:
            key = gen_key(authors, year, name)
        else:
            key = placeholder_key(year, name)

        missing.append({"item_name": key, "title": name, "authors": authors,
                        "year": year, "content": ""})
    return missing


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve arXiv BibTeX entries to published versions"
    )
    parser.add_argument("--bib", default=DEFAULT_BIB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-missing", action="store_true",
                        help="Only process arXiv entries; skip xlsx entries with no key")
    args = parser.parse_args()

    with open(args.bib) as f:
        bib_text = f.read()

    arxiv_entries = get_arxiv_entries(bib_text)
    missing_entries = [] if args.skip_missing else get_missing_bib_entries(bib_text)

    # Deduplicate
    seen: set[str] = set()
    candidates = []
    for e in arxiv_entries + missing_entries:
        if e["item_name"] not in seen:
            seen.add(e["item_name"])
            candidates.append(e)

    attempts = load_attempts()
    candidates = sort_by_attempts(candidates, attempts)

    print(f"Found {len(arxiv_entries)} arXiv entries in {os.path.basename(args.bib)}")
    if not args.skip_missing:
        print(f"Found {len(missing_entries)} xlsx entries with no BibTeX")
    n_deprio = sum(1 for e in candidates if attempts.get(e["item_name"], 0) >= _DEPRIORITIZE_AFTER)
    if n_deprio:
        print(f"  ({n_deprio} entries with ≥{_DEPRIORITIZE_AFTER} prior attempts sorted last)")
    print(f"Resolving {len(candidates)} candidates...\n")

    resolved_bibs: list[str] = []
    report: list[tuple[str, str, bool]] = []

    for entry in candidates:
        key = entry["item_name"]
        title = entry["title"]
        arxiv_id = _get_arxiv_id(entry)
        content = entry.get("content", "")

        arxiv_label = f"arXiv:{arxiv_id}" if arxiv_id else "(no arXiv ID)"
        print(f"  [{key[:42]}] {arxiv_label:<22}", end=" ", flush=True)

        bib, source = resolve(title, arxiv_id, key, content)
        print(f"→ {source}")

        attempts[key] = attempts.get(key, 0) + 1
        save_attempts(attempts)

        report.append((key, source, bool(bib)))
        if bib:
            resolved_bibs.append(bib)
        time.sleep(0.5)

    with open(args.output, "w") as f:
        f.write("\n\n".join(resolved_bibs))
        if resolved_bibs:
            f.write("\n")

    print(f"\n{'─' * 72}")
    print(f"{'':2}{'BibKey':<44} {'Source'}")
    print("─" * 72)
    for key, source, ok in report:
        print(f"  {'✓' if ok else '✗'} {key:<42} {source}")
    print("─" * 72)
    written = sum(1 for _, _, ok in report if ok)
    print(f"\n{written}/{len(candidates)} entries written to {args.output}")
    print("Review and copy desired entries into orig.bib manually.")


if __name__ == "__main__":
    main()
