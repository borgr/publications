#!/usr/bin/env python3
"""Scrape a Google Scholar profile and write citations.csv.

Usage:
    python fetch_citations.py [USER_ID_OR_URL] [-o OUTPUT]

Defaults to the profile and citations.csv configured in config.py.

Dependencies: beautifulsoup4  (curl is used for HTTP, no requests library needed)
    pip install beautifulsoup4
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

import config
from citations_io import read_citation_rows, write_citation_rows

DEFAULT_USER_ID = config.SCHOLAR_USER_ID
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "citations.csv")

# Scholar's default page size; mirrors what a browser's "Show more" click fetches
_PAGE_SIZE = 20
_MAX_RETRIES = 4

# Refuse to overwrite citations.csv if the paper count drops by more than this.
# A partial scrape looks exactly like papers having been removed, and the cost of
# getting it wrong is asymmetric: papers past a failed page silently read as zero.
_MAX_SHRINK = 0.10

# curl flags that produce a browser-like request (avoids Python TLS fingerprint blocking)
_CURL_FLAGS = [
    "--silent",
    "--compressed",  # Accept-Encoding: br, gzip, deflate
    "--max-time", "30",
    "-A", (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "-H", "Accept-Language: en-US,en;q=0.9",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Referer: https://scholar.google.com/",
    # Persist cookies across requests in the same run
    "--cookie-jar", "/tmp/_scholar_cookies.txt",
    "--cookie", "/tmp/_scholar_cookies.txt",
]


def _check_curl() -> None:
    if not shutil.which("curl"):
        raise RuntimeError("curl is required but not found in PATH.")


def _extract_user_id(user_id_or_url: str) -> str:
    if "scholar.google.com" in user_id_or_url:
        params = parse_qs(urlparse(user_id_or_url).query)
        ids = params.get("user")
        if not ids:
            raise ValueError(f"Could not extract user= from URL: {user_id_or_url}")
        return ids[0]
    return user_id_or_url.strip()


def _curl_get(url: str) -> tuple[int, str]:
    """Fetch a URL with curl. Returns (http_status_code, body)."""
    result = subprocess.run(
        ["curl", *_CURL_FLAGS, "--write-out", "\n__STATUS__%{http_code}", url],
        capture_output=True,
        text=True,
        timeout=40,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl subprocess failed (exit {result.returncode}): {result.stderr.strip()}")

    # Split off the status code we appended
    *body_parts, status_line = result.stdout.rsplit("\n__STATUS__", 1)
    body = "\n__STATUS__".join(body_parts)  # rejoin in case body itself had the marker
    try:
        status = int(status_line.strip())
    except ValueError:
        raise RuntimeError(f"Unexpected curl output format: {result.stdout[-200:]}")
    return status, body


def _fetch_page(user_id: str, start: int) -> str:
    url = (
        "https://scholar.google.com/citations"
        f"?user={user_id}&hl=en&cstart={start}&pagesize={_PAGE_SIZE}"
    )
    for attempt in range(_MAX_RETRIES):
        status, body = _curl_get(url)
        if status == 200:
            return body
        if status == 429:
            wait = 30 * (2 ** attempt)
            print(
                f"  Rate limited (429). Waiting {wait}s before retry {attempt + 1}/{_MAX_RETRIES}…",
                flush=True,
            )
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {status} from Scholar for {url}")
    raise RuntimeError(f"Failed to fetch page after {_MAX_RETRIES} attempts (persistent 429).")


def _extract_scholar_id(anchor) -> str:
    """Return the stable per-paper Scholar ID from a title anchor's href.

    The href carries `citation_for_view=USER:PUBID` (e.g. `8b8I...:RHpTSmoSYBkC`).
    That ID survives title edits and re-rankings, so recording it is what lets
    the citation join be an exact lookup instead of a title-similarity guess.
    Returns "" if absent, which degrades to title matching rather than failing.
    """
    if anchor is None:
        return ""
    href = anchor.get("href") or ""
    params = parse_qs(urlparse(href).query)
    return (params.get("citation_for_view") or [""])[0].strip()


def _parse_page(html: str) -> list[dict]:
    """Parse one page of Scholar HTML into a list of paper dicts.

    CSS selectors used (stable since ~2015):
      tr.gsc_a_tr   — paper row
      .gsc_a_at     — title anchor (also carries citation_for_view=USER:PUBID)
      .gs_gray      — authors (index 0) and venue (index 1)
      .gsc_a_ac     — citation count anchor
      .gsc_a_y span — year span
    """
    soup = BeautifulSoup(html, "html.parser")

    body_text = soup.get_text(" ", strip=True).lower()
    if "captcha" in body_text or "unusual traffic" in body_text:
        raise RuntimeError(
            "Google Scholar returned a CAPTCHA / unusual-traffic page. "
            "Wait a while and try again from a different network."
        )

    papers = []
    for row in soup.select("tr.gsc_a_tr"):
        title_el = row.select_one(".gsc_a_at")
        gray_els = row.select(".gs_gray")
        cite_el = row.select_one(".gsc_a_ac")
        year_el = row.select_one(".gsc_a_y span")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        papers.append(
            {
                "title": title,
                "authors": gray_els[0].get_text(strip=True) if len(gray_els) > 0 else "",
                "venue": gray_els[1].get_text(strip=True) if len(gray_els) > 1 else "",
                "citations": cite_el.get_text(strip=True) if cite_el else "",
                "year": year_el.get_text(strip=True) if year_el else "",
                "scholar_id": _extract_scholar_id(title_el),
            }
        )
    return papers


def _parse_profile_stats(html: str) -> dict | None:
    """Extract total citations and h-index from the Scholar profile page HTML.

    Returns a dict with keys 'citations', 'h_index', 'i10_index', or None if
    the stats table is not found (structure may have changed).

    CSS selectors used (stable since ~2015):
      table#gsc_rsb_st   — the stats summary table
      td.gsc_rsb_sc1     — row label (Citations / h-index / i10-index)
      td.gsc_rsb_std     — row values (all-time, last 5 years)
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="gsc_rsb_st")
    if not table:
        return None
    key_map = {"citations": "citations", "h-index": "h_index", "i10-index": "i10_index"}
    stats: dict = {}
    for row in table.find_all("tr"):
        label_el = row.find("td", class_="gsc_rsb_sc1")
        value_els = row.find_all("td", class_="gsc_rsb_std")
        if not label_el or not value_els:
            continue
        label = label_el.get_text(strip=True).lower()
        key = key_map.get(label)
        if key is None:
            continue
        try:
            stats[key] = int(value_els[0].get_text(strip=True).replace(",", ""))
        except ValueError:
            pass
    return stats if stats else None


def write_stats(stats: dict, path: str) -> None:
    """Atomically write profile stats to a JSON file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def scrape_profile(user_id: str, delay: float = 3.0) -> tuple[list[dict], dict | None]:
    """Fetch all publications and profile stats from a Scholar profile.

    Returns (papers, stats) where stats contains citations, h_index, i10_index.
    """
    _check_curl()
    all_papers: list[dict] = []

    # Warm-up request: loads the profile page for cookies AND profile-level stats
    print("  Loading profile page…", flush=True)
    _, profile_html = _curl_get(f"https://scholar.google.com/citations?user={user_id}&hl=en")
    stats = _parse_profile_stats(profile_html)
    if stats:
        print(f"  Profile stats: citations={stats.get('citations')}, "
              f"h-index={stats.get('h_index')}", flush=True)
    else:
        print("  Warning: could not parse profile stats (Scholar HTML may have changed).",
              flush=True)
    time.sleep(delay)

    start = 0
    while True:
        print(f"  Fetching records {start + 1}–{start + _PAGE_SIZE}…", flush=True)
        html = _fetch_page(user_id, start)
        page = _parse_page(html)

        if not page and start > 0:
            # An empty page after a full one is ambiguous: either the end of the
            # profile, or a hiccup. Treating it as the end silently truncated the
            # profile, and since the result overwrites citations.csv, every paper
            # past the gap then reported zero citations. Retry before believing it.
            print("    empty page after a full one — retrying once…", flush=True)
            time.sleep(max(delay, 5.0))
            page = _parse_page(_fetch_page(user_id, start))

        if not page:
            break

        all_papers.extend(page)

        if len(page) < _PAGE_SIZE:
            break  # reached the last page

        start += _PAGE_SIZE
        time.sleep(delay)

    # Warn if citation counts are universally missing (possible selector change)
    if all_papers and not any(p["citations"] for p in all_papers):
        print(
            "WARNING: no citation counts found — Scholar's HTML structure may have changed.",
            file=sys.stderr,
        )
    # The stable IDs are what keep the citation join exact; losing them silently
    # would degrade every later run back to title guessing.
    if all_papers and not any(p.get("scholar_id") for p in all_papers):
        print(
            "WARNING: no citation_for_view IDs found — Scholar's link format may have "
            "changed. The citation join will fall back to title matching.",
            file=sys.stderr,
        )

    return all_papers, stats


def write_csv(papers: list[dict], output_path: str) -> None:
    """Write citations.csv (one row per paper, atomic)."""
    write_citation_rows(papers, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape a Google Scholar profile to citations.csv"
    )
    parser.add_argument(
        "user",
        nargs="?",
        default=DEFAULT_USER_ID,
        help=f"Scholar user ID or full profile URL (default: {DEFAULT_USER_ID})",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Write even if the paper count dropped sharply (use when the profile "
             "genuinely lost papers)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    user_id = _extract_user_id(args.user)
    print(f"Fetching Scholar profile for user: {user_id}")

    papers, stats = scrape_profile(user_id)
    print(f"Found {len(papers)} papers.")

    if not papers:
        raise RuntimeError(
            "Fetched 0 papers — Scholar HTML may have changed or the profile is empty. "
            f"{args.output} was NOT overwritten."
        )

    # A partial scrape is far more dangerous than a failed one, because the result
    # overwrites citations.csv and every paper past the gap silently reports zero
    # citations from then on. Compare against what we already had.
    previous = read_citation_rows(args.output)
    if previous and not args.allow_shrink:
        floor = int(len(previous) * (1 - _MAX_SHRINK))
        if len(papers) < floor:
            raise RuntimeError(
                f"Fetched {len(papers)} papers but {os.path.basename(args.output)} "
                f"already had {len(previous)} — a drop of more than "
                f"{_MAX_SHRINK:.0%} usually means a page failed to load, not that "
                f"papers disappeared. {args.output} was NOT overwritten.\n"
                f"  Re-run; if the profile genuinely shrank, pass --allow-shrink."
            )

    write_csv(papers, args.output)
    print(f"Saved to {args.output}")

    if stats:
        stats_path = os.path.join(os.path.dirname(os.path.abspath(args.output)),
                                  "profile_stats.json")
        write_stats(stats, stats_path)
        print(f"Profile stats saved to {stats_path}")


if __name__ == "__main__":
    main()
