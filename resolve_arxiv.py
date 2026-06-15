#!/usr/bin/env python3
"""Resolve arXiv BibTeX entries to published versions.

For each arXiv entry in orig.bib, and each entry in Contributions_table.xlsx
with no BibTeX key, fetches the best available BibTeX:
  1. DBLP title search  →  published @inproceedings/@article if indexed
  2. S2 externalIds     →  ACL Anthology or OpenReview BibTeX
  3. DBLP CoRR entry    →  clean arXiv BibTeX as fallback
  4. arXiv export API   →  last resort if DBLP has no entry at all

Usage:
    python resolve_arxiv.py [--bib orig.bib] [--output resolved.bib] [--skip-missing]

Dependencies: beautifulsoup4 (already in requirements.txt), curl
"""

import argparse
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

from augment_bib import parse_bibtex, read_df

DEFAULT_BIB = os.path.join(FILE_DIR, "orig.bib")
DEFAULT_OUTPUT = os.path.join(FILE_DIR, "resolved.bib")

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


_s2_disabled = False  # set on first 429; skips S2 for the rest of the run


def _http_get_json(url: str, retries: int = 2) -> dict | None:
    global _s2_disabled
    if _s2_disabled:
        return None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "resolve-arxiv-bib/1.0"})
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            exc_str = str(exc)
            is_429 = "429" in exc_str or getattr(exc, "code", None) == 429
            if is_429:
                if attempt == 0:
                    print(f"\n    S2 rate-limited, waiting 30s…", end="", flush=True)
                    time.sleep(30)
                    continue
                print(f"\n    S2 rate-limited again — disabling S2 for this run", end="", flush=True)
                _s2_disabled = True
                return None
            if attempt == retries - 1:
                print(f"\n    HTTP error ({exc})", file=sys.stderr, end="")
                return None
            time.sleep(3)
    return None


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
    return re.sub(r'(@\w+\s*\{)\s*[^,\s]+\s*,', rf'\g<1>{new_key},', bibtex, count=1)


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


def pick_published(bibtex_list: list[str]) -> tuple[str | None, str | None]:
    """Return (first_published, first_corr) from DBLP results."""
    published = corr = None
    for bib in bibtex_list:
        if _is_corr(bib):
            if corr is None:
                corr = bib
        else:
            if published is None:
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
        f"  title = {{{title}}},\n"
        f"  author = {{{authors}}},\n"
        f"  booktitle = {{{venue}}},\n"
        f"  year = {{{year}}},\n"
        f"  url = {{https://openreview.net/forum?id={forum_id}}}\n"
        f"}}"
    )


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
        f"  title = {{{title}}},\n"
        f"  author = {{{authors_str}}},\n"
        f"  year = {{{year}}},\n"
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
            existing_content: str = "") -> tuple[str, str]:
    """Return (bibtex_string, source_label)."""
    corr_bib = None

    # Step 1 — DBLP title search
    dblp_results = search_dblp(title)
    published_bib, corr_bib = pick_published(dblp_results)
    if published_bib:
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

    # Step 3 — arXiv fallback
    if corr_bib:
        return _replace_key(corr_bib, original_key), "arXiv (DBLP/CoRR)"
    if arxiv_id:
        return fetch_arxiv_bib(arxiv_id, original_key), "arXiv (export API)"

    return "", "not found"


# ── In-place bib update ───────────────────────────────────────────────────────

_PUBLISHED_SOURCES = {"DBLP", "ACL Anthology", "OpenReview"}


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
    for key, new_bib, source in updates:
        if source not in _PUBLISHED_SOURCES:
            continue
        pattern = re.compile(
            r'@\w+\s*\{' + re.escape(key) + r',.*?\n\}',
            re.DOTALL,
        )
        new_text, count = pattern.subn(new_bib.rstrip('\n'), bib_text, count=1)
        if count:
            bib_text = new_text
            n_replaced += 1
    n_appended = 0
    for _key, new_bib in new_entries:
        bib_text = bib_text.rstrip('\n') + '\n\n' + new_bib + '\n'
        n_appended += 1
    return bib_text, n_replaced, n_appended


# ── Candidate discovery ───────────────────────────────────────────────────────

def get_arxiv_entries(bib_text: str) -> list[dict]:
    return [e for e in parse_bibtex(bib_text) if _is_arxiv(e)]


def get_missing_bib_entries(bib_text: str) -> list[dict]:
    """xlsx rows whose Bib key is absent from orig.bib."""
    try:
        df = read_df()
    except Exception as exc:
        print(f"Warning: could not read Contributions_table.xlsx: {exc}", file=sys.stderr)
        return []
    existing_keys = {e["item_name"] for e in parse_bibtex(bib_text)}
    missing = []
    for _, row in df.iterrows():
        bib_key = str(row.get("Bib", "")).strip()
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        if not bib_key or bib_key.lower() in ("nan", "none") or bib_key not in existing_keys:
            safe_key = bib_key if bib_key and bib_key.lower() not in ("nan", "none") else ""
            missing.append({
                "item_name": safe_key or f"_missing_{re.sub(r'[^A-Za-z0-9]', '', name)[:30]}",
                "title": name,
                "content": "",
            })
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

    print(f"Found {len(arxiv_entries)} arXiv entries in {os.path.basename(args.bib)}")
    if not args.skip_missing:
        print(f"Found {len(missing_entries)} xlsx entries with no BibTeX")
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
