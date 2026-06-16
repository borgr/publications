#!/usr/bin/env python3
"""Scrape a Google Scholar profile and write citations.csv.

Usage:
    python fetch_citations.py [USER_ID_OR_URL] [-o OUTPUT]

Defaults to Leshem Choshen's profile and citations.csv next to this file.

Dependencies: beautifulsoup4  (curl is used for HTTP, no requests library needed)
    pip install beautifulsoup4
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

DEFAULT_USER_ID = "8b8IhUYAAAAJ"
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "citations.csv")

# Scholar's default page size; mirrors what a browser's "Show more" click fetches
_PAGE_SIZE = 20
_MAX_RETRIES = 4

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


def _parse_page(html: str) -> list[dict]:
    """Parse one page of Scholar HTML into a list of paper dicts.

    CSS selectors used (stable since ~2015):
      tr.gsc_a_tr   — paper row
      .gsc_a_at     — title anchor
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
            }
        )
    return papers


def scrape_profile(user_id: str, delay: float = 3.0) -> list[dict]:
    """Fetch all publications from a Scholar profile, paginating as needed."""
    _check_curl()
    all_papers: list[dict] = []

    # Warm-up request: load the bare profile page to populate the cookie jar
    print("  Loading profile page for cookies…", flush=True)
    _curl_get(f"https://scholar.google.com/citations?user={user_id}&hl=en")
    time.sleep(delay)

    start = 0
    while True:
        print(f"  Fetching records {start + 1}–{start + _PAGE_SIZE}…", flush=True)
        html = _fetch_page(user_id, start)
        page = _parse_page(html)

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

    return all_papers


def write_csv(papers: list[dict], output_path: str) -> None:
    """Write the 3-row-per-paper format used by citations.csv (atomic write)."""
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Title", "Cited by", "Year"])
            for p in papers:
                writer.writerow([p["title"], p["citations"], p["year"]])
                writer.writerow([p["authors"], "", ""])
                writer.writerow([p["venue"], "", ""])
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape a Google Scholar profile to citations.csv"
    )
    parser.add_argument(
        "user",
        nargs="?",
        default=DEFAULT_USER_ID,
        help="Scholar user ID or full profile URL (default: Leshem Choshen)",
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

    papers = scrape_profile(user_id)
    print(f"Found {len(papers)} papers.")

    if not papers:
        raise RuntimeError(
            "Fetched 0 papers — Scholar HTML may have changed or the profile is empty. "
            f"{args.output} was NOT overwritten."
        )

    write_csv(papers, args.output)
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    main()
