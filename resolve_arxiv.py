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

A result is only ever a *candidate*. Which candidates may replace an existing
orig.bib entry, and what of it is allowed to change, is bib_edit's decision --
nothing here writes to the bibliography.

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

import config
from bib_edit import (
    _get_arxiv_id,
    _is_corr,
    _replace_key,
    get_arxiv_entries,
    get_missing_bib_entries,
    update_bib_inplace,
)
from bib_utils import (
    escape_field_value,
    extract_field,
    lists_author,
    normalize_text,
)
from identity import harvest_ids_from_bibtex, harvest_ids_from_s2

DEFAULT_BIB    = os.path.join(FILE_DIR, "orig.bib")
DEFAULT_OUTPUT = os.path.join(FILE_DIR, "resolved.bib")
ATTEMPTS_PATH  = os.path.join(FILE_DIR, "resolve_attempts.json")

# Entries tried >= this many times are sorted to the end of each run.
# They still run (not skipped), but fresh entries get S2 quota first.
_DEPRIORITIZE_AFTER = 5

# The source label for "nothing was found, but a source we needed was silent".
# Distinct from "not found", which is a real negative answer from every source.
UNANSWERED = "unknown (a source did not reply)"

_CURL_FLAGS = [
    "--silent", "--compressed", "--max-time", "20",
    "-A", "resolve-arxiv-bib/1.0",
    "--cookie-jar", "/tmp/_resolve_arxiv_cookies.txt",
    "--cookie", "/tmp/_resolve_arxiv_cookies.txt",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────
#
# A request that failed is not an answer.
#
# Everything below distinguishes "the source says this paper is not published"
# from "the source did not reply", because conflating them silently downgrades a
# paper: DBLP rate-limited a long run, `_curl_get` returned "" for the refusal,
# `search_dblp` read that as zero hits, and an ACL 2026 paper DBLP knows about was
# re-resolved to `OpenAlex (preprint)` and cited in the CV as a preprint. Nothing
# was logged, because from the resolver's point of view nothing went wrong.
#
# Unanswered requests are counted so the caller can refuse to record a negative:
# step 3 neither burns a retry counter nor marks itself done when a source it
# needed never replied, so the next run asks again.
_net_state = {"unanswered": 0}

_CURL_TRIES = 3
_CURL_BACKOFF_SECONDS = 2.0


def unanswered_lookups() -> int:
    """How many requests got no usable reply since the last reset."""
    return _net_state["unanswered"]


def reset_unanswered_lookups() -> None:
    _net_state["unanswered"] = 0


def _note_unanswered(what: str) -> None:
    _net_state["unanswered"] += 1
    print(f"\n    {what} did not reply — treating the answer as unknown, "
          f"not as 'unpublished'", end="", flush=True)


def _curl_get(url: str, accept=None, tries: int = _CURL_TRIES) -> str | None:
    """GET a URL's body, or None when the request could not be answered.

    None and "" mean different things and callers must keep them apart: "" is a
    source that replied that it has nothing (DBLP answers a no-hit title search
    with an empty 200), None is a source that did not reply at all. Only the
    first is evidence about the paper.

    `accept` is an optional predicate deciding whether a body counts as a reply.
    It exists because a refusal can arrive wearing an answer's clothes: DBLP
    serves an HTML error page, with status 200, when it is rate-limiting.
    """
    for attempt in range(tries):
        replied, body = False, ""
        try:
            result = subprocess.run(
                ["curl", *_CURL_FLAGS, url],
                capture_output=True, text=True, timeout=30,
            )
            replied, body = result.returncode == 0, result.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        # An empty body from a successful request is an answer: the source has
        # nothing. Retrying it would cost three requests and six seconds for
        # every paper no source indexes yet.
        if replied and (not body or accept is None or accept(body)):
            return body
        if attempt < tries - 1:
            time.sleep(_CURL_BACKOFF_SECONDS * (attempt + 1))
    _note_unanswered(_host_of(url))
    return None


def _host_of(url: str) -> str:
    m = re.match(r'https?://([^/]+)', url)
    return m.group(1) if m else url


# Semantic Scholar rate limits, per its own documentation:
#
#   unauthenticated  "rate-limited to 1000 requests per second shared among all
#                    unauthenticated users", and "may also be further throttled
#                    during periods of heavy use"
#   with an API key  "The introductory rate limit for an API key is 1 RPS"
#
# The unauthenticated number looks generous but is a *global* pool shared with
# every other anonymous caller, which is why a long run gets 429s almost
# immediately -- on a real run, from the second paper onward. A free key is
# slower on paper (1 RPS) but it is 1 RPS reserved for you, so it actually
# completes. Request one at https://www.semanticscholar.org/product/api and put it
# where KEY_FILE points, below.
#
# Without a key this still works, just with more waiting: a 429 pauses S2 for a
# cooldown rather than disabling it for the whole run. Disabling it was the worse
# bug, because the ACL Anthology and OpenReview are both reached *through* S2's
# externalIds, so losing S2 lost all three.
_S2_COOLDOWN_SECONDS = 120
_s2_state = {"blocked_until": 0.0}


# Where the key is looked for, in order. config.py has a slot for it and is the
# obvious place to put one, which is the problem: config.py is tracked and this
# repository is public, so filling that slot in works perfectly and then publishes
# the key on the next `git add -A`. The slot is kept for a fork that keeps its
# config private, and listed last.
#
# The file is what the unattended run reads. An exported variable covers a terminal
# and covers CI, but launchd hands a job PATH and HOME and none of the shell's
# exports -- so a key in .zshrc would cover every run except the weekly one that
# does the most lookups, which is the run whose throttling nobody is watching.
#
# A file rather than the keychain, unlike the Overleaf token: that one is a
# password git itself has to answer a prompt with, while this is a header value
# this code sends, so there is nothing to hand to a credential helper. Outside
# every worktree either way.
KEY_FILE = os.path.expanduser("~/.config/publications/s2_api_key")

ENV_SOURCE = "the S2_API_KEY environment variable"
FILE_SOURCE = KEY_FILE
CONFIG_SOURCE = "config.py"


def s2_api_key_source() -> tuple[str, str]:
    """The Semantic Scholar API key and where it was found, or ("", "") for none.

    The source is reported rather than just the key, so a run says which of the
    three places is in play. Two of them are easy to edit without effect -- a
    variable exported in a shell the scheduled run never sees, or a config.py slot
    shadowed by a key file left behind from before -- and the failure either way is
    invisible: the run is simply throttled, as if no key had ever been requested.
    """
    key = os.environ.get("S2_API_KEY", "").strip()
    if key:
        return key, ENV_SOURCE
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError:
        key = ""
    if key:
        return key, FILE_SOURCE
    try:
        import config
        key = (getattr(config, "S2_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    return (key, CONFIG_SOURCE) if key else ("", "")


def s2_api_key() -> str:
    """The Semantic Scholar API key, or "" if none is configured."""
    return s2_api_key_source()[0]


def s2_available() -> bool:
    """False while S2 is in a rate-limit cooldown."""
    if time.time() < _s2_state["blocked_until"]:
        return False
    if _s2_state["blocked_until"]:
        print("\n    S2 cooldown over, using it again", end="", flush=True)
        _s2_state["blocked_until"] = 0.0
    return True


def _http_get_json(url: str, retries: int = 2) -> dict | None:
    """GET JSON, pausing a rate-limited source rather than abandoning it.

    None is always "no answer", never "no such paper", and is counted as such --
    including the cooldown case, where the request is not even attempted. Since
    the ACL Anthology and OpenReview are both reached through S2, a cooldown
    means three sources are silent rather than one.
    """
    if not s2_available():
        _note_unanswered(f"{_host_of(url)} (in cooldown)")
        return None
    headers = {"User-Agent": "resolve-arxiv-bib/1.0"}
    key = s2_api_key()
    if key:
        headers["x-api-key"] = key
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
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
                _note_unanswered(_host_of(url))
                return None
            if attempt == retries - 1:
                print(f"\n    HTTP error ({exc})", file=sys.stderr, end="")
                _note_unanswered(_host_of(url))
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


# ── Reading a source's answer ─────────────────────────────────────────────────

def _simplify_title(t: str) -> str:
    return re.sub(r'[\W_]+', '', t.lower())


def _dblp_title(bibtex: str) -> str:
    """The whole title value, brace groups included.

    `[^}"]+` stopped at the first closing brace, and DBLP protects capitals
    inside titles almost everywhere -- `{A} Benchmark`, `Findings of the
    {B}aby{LM}`, `\\texttt{Holmes}`. So the "title" this returned was often the
    first few words, which then failed pick_published's 0.72 guard and discarded
    a correctly-found published version: Holmes came back as `\\texttt{Holmes`
    and scored 0.16 against its own title, so the TACL entry was rejected and the
    paper stayed a CoRR preprint even though DBLP had the journal version.
    """
    return extract_field(bibtex, "title")


def _extract_openreview_id(text: str) -> str | None:
    m = re.search(r'openreview\.net/forum\?id=([A-Za-z0-9_-]+)', text)
    return m.group(1) if m else None


# ── DBLP ──────────────────────────────────────────────────────────────────────

def search_dblp(title: str) -> list[str] | None:
    """Search DBLP by title. Returns up to 5 BibTeX strings, or None if it did not reply.

    `[]` means DBLP has no record of this paper -- it answers a no-hit search
    with an empty body. `None` means DBLP refused to answer, which says nothing
    about the paper and must not be read as `[]`.
    """
    url = f"https://dblp.org/search/publ/api?q={quote(title)}&format=bib&h=5"
    # An HTML body is DBLP's rate-limit page, served with status 200. Rejecting it
    # here makes _curl_get retry it and, if it persists, report no answer.
    raw = _curl_get(url, accept=lambda body: not body.lstrip().startswith("<"))
    if raw is None:
        return None
    entries = re.split(r'\n(?=@)', raw.strip())
    return [e.strip() for e in entries if e.strip().startswith("@")]


def _bib_year(bibtex: str) -> int | None:
    m = re.search(r'\byear\s*=\s*\{?\s*(\d{4})', bibtex)
    return int(m.group(1)) if m else None


# Above this, two titles are the same title, so a publication lag is expected --
# ComPEFT's journal version is two years after its preprint under an identical
# title. Below it, agreement on the year is required as well.
_SAME_TITLE_RATIO = 0.95


def pick_published(bibtex_list: list[str], query_title: str = "",
                   query_year: int | None = None) -> tuple[str | None, str | None]:
    """Return (first_published, first_corr) from DBLP results.

    When query_title is given, published entries whose title similarity is below
    0.72 are skipped — this guards against DBLP returning a different paper.

    Similarity alone is not enough: difflib rewards a long common subsequence
    however much *extra* text there is, and the extra text is often exactly what
    distinguishes two papers. "Holistic Evaluation of Language Models" scores
    0.76 against "SEA-HELM: Southeast Asian Holistic Evaluation of Language
    Models", a different paper three years later. So a merely-similar title also
    has to agree on the year.

    And whatever the titles say, an entry that does not list the author is not a
    version of the author's paper. Without that test this function accepted a 2022
    ISAIM invited talk by one of a Nature paper's twenty co-authors as that paper's
    published version, on a title similarity of 0.86 -- and the CV printed it.
    """
    published = corr = None
    for bib in bibtex_list:
        if not lists_author(bib, config.AUTHOR_NAME):
            continue
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
                    year = _bib_year(bib)
                    if (ratio < _SAME_TITLE_RATIO and query_year and year
                            and not 0 <= year - query_year <= 1):
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
    # cdate is epoch *milliseconds*, so its first four digits are "1700", not a
    # year -- every entry from this source was dated 1700.
    year = ""
    try:
        year = str(time.gmtime(int(data["cdate"]) / 1000).tm_year)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        year = str(_val("year") or "")
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

def fetch_arxiv_bib(arxiv_id: str, original_key: str,
                    known_title: str = "") -> str:
    """Parse arXiv abstract page for metadata and build a @misc entry.

    `known_title` is the title the caller already has, from the publications
    table or the existing bib entry. It matters because this is the last
    fallback: without a title the entry fails `is_wellformed_entry` and is
    refused by `update_bib_inplace`, so a paper whose abstract page could not be
    fetched -- arXiv rate-limiting a long run, or no network -- was dropped from
    the CV entirely and retried forever, even though its title was known all
    along.
    """
    from bs4 import BeautifulSoup

    html = _curl_get(f"https://arxiv.org/abs/{arxiv_id}")
    if not html:
        return _bare_arxiv_bib(arxiv_id, original_key, known_title)

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
        return _bare_arxiv_bib(arxiv_id, original_key, known_title)

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


def _bare_arxiv_bib(arxiv_id: str, key: str, title: str = "") -> str:
    m = re.match(r'(\d{2})', arxiv_id)
    year = f"20{m.group(1)}" if m else ""
    return (
        f"@misc{{{key},\n"
        f"  title = {{{escape_field_value(title)}}},\n"
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
    unanswered_before = unanswered_lookups()

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
    # Past this point `title` is known to be usable, so it is worth passing to
    # the fallback as the title of last resort.

    # The entry's own year, used to reject a similarly-titled different paper.
    year_m = (re.search(r'\byear\s*=\s*\{?\s*(\d{4})', existing_content)
              or re.search(r'\b(20\d{2})\b', existing_content))

    # Step 1 — DBLP title search. `None` is "DBLP did not reply", which is not the
    # same as an empty result list and must not fall through to a preprint as
    # though DBLP had said the paper is unpublished.
    dblp_results = search_dblp(title)
    if dblp_results:
        published_bib, corr_bib = pick_published(
            dblp_results, query_title=title,
            query_year=int(year_m.group(1)) if year_m else None)
        if published_bib:
            _remember(**harvest_ids_from_bibtex(published_bib))
            return _replace_key(published_bib, original_key), "DBLP"
    time.sleep(1.0)

    # Step 2 — S2 to get ACL / OpenReview IDs
    s2_data = None
    if arxiv_id:
        s2_data = query_s2_by_arxiv(arxiv_id)
    else:
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
                return bib, "ACL Anthology"

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
        return (fetch_arxiv_bib(arxiv_id, original_key, known_title=title),
                "arXiv (export API)")

    # Nothing found -- but "no source has it" and "no source answered" are
    # different conclusions, and only the first is worth recording as a failed
    # attempt or reporting as a paper needing a hand-pasted entry.
    if unanswered_lookups() > unanswered_before:
        return "", UNANSWERED
    return "", "not found"





def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Resolve arXiv BibTeX entries to published versions"
    )
    parser.add_argument("--bib", default=DEFAULT_BIB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-missing", action="store_true",
                        help="Only process arXiv entries; skip table rows with no key")
    parser.add_argument("--in-place", action="store_true",
                        help="also write the published venues back into --bib "
                             "(title, author and pretitle are left alone; diff it)")
    args = parser.parse_args(argv)

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
        print(f"Found {len(missing_entries)} table rows with no BibTeX")
    n_deprio = sum(1 for e in candidates if attempts.get(e["item_name"], 0) >= _DEPRIORITIZE_AFTER)
    if n_deprio:
        print(f"  ({n_deprio} entries with ≥{_DEPRIORITIZE_AFTER} prior attempts sorted last)")
    print(f"Resolving {len(candidates)} candidates...\n")

    resolved_bibs: list[str] = []
    report: list[tuple[str, str, bool]] = []
    updates: list[tuple[str, str, str]] = []

    for entry in candidates:
        key = entry["item_name"]
        title = entry["title"]
        arxiv_id = _get_arxiv_id(entry)
        content = entry.get("content", "")

        arxiv_label = f"arXiv:{arxiv_id}" if arxiv_id else "(no arXiv ID)"
        print(f"  [{key[:42]}] {arxiv_label:<22}", end=" ", flush=True)

        before = unanswered_lookups()
        bib, source = resolve(title, arxiv_id, key, content)
        print(f"→ {source}")

        # Only a lookup that actually completed counts as an attempt. Counting a
        # network failure would deprioritize the paper for a problem that is not
        # the paper's, and an outage across one run is enough to push every
        # unresolved entry past _DEPRIORITIZE_AFTER at once.
        if bib or unanswered_lookups() == before:
            attempts[key] = attempts.get(key, 0) + 1
            save_attempts(attempts)

        report.append((key, source, bool(bib)))
        if bib:
            resolved_bibs.append(bib)
            # Only an entry that is still a preprint has anything to gain, and
            # only a published record has anything to give.
            if _is_corr(content) and not _is_corr(bib):
                updates.append((key, bib, source))
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

    if not args.in_place:
        print(f"{len(updates)} preprint entries have a published version. To write "
              f"their venues into\n{os.path.basename(args.bib)} (leaving title, "
              f"author and pretitle alone), rerun with --in-place.")
        return
    new_text, n_replaced, _ = update_bib_inplace(bib_text, updates, [])
    if n_replaced:
        with open(args.bib, "w") as f:
            f.write(new_text)
    print(f"{n_replaced} entries upgraded in place in {args.bib} — `git diff` it.")


if __name__ == "__main__":
    main()
