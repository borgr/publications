#!/usr/bin/env python3
"""Refresh venue rankings and metrics in venues.yaml from their real sources.

Two sources, chosen because they are the ones the numbers actually came from and
because neither needs an API key:

  Google Scholar Metrics  the ranked top-20 list per subject category. This is
                          the origin of the "Nth of 20 in computational
                          linguistics conferences by Google Scholar" phrasing,
                          and it also yields each venue's h5-index.
  OpenAlex /sources       `2yr_mean_citedness` and `h_index` per venue. The free
                          analogue of an impact factor -- the actual Journal
                          Impact Factor is Clarivate's proprietary metric and
                          cannot be looked up, which is why journal prose stays
                          hand-written and `manual: true`.

A venue marked `manual: true` keeps its description; only its `metrics` block is
updated. Everything else has its description regenerated from the fresh numbers.

    python scripts/refresh_venues.py
    python scripts/refresh_venues.py --dry-run
    python scripts/refresh_venues.py --source scholar
"""

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from venues import Venues, VENUES_PATH  # noqa: E402

try:
    import config
    CONTACT = getattr(config, "CONTACT_EMAIL", "") or ""
except Exception:
    CONTACT = ""

SCHOLAR_TOP_VENUES = "https://scholar.google.com/citations?view_op=top_venues&hl=en&vq="
OPENALEX_SOURCES = "https://api.openalex.org/sources"

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")

_ORDINALS = {1: "st", 2: "nd", 3: "rd"}


def ordinal(n):
    """1 -> '1st'. The old hardcoded data said '1th', which is why this exists."""
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    return f"{n}{_ORDINALS.get(n % 10, 'th')}"


def _curl(url, browser=False):
    cmd = ["curl", "--silent", "--compressed", "--max-time", "30"]
    if browser:
        cmd += ["-A", _BROWSER_UA, "-H", "Accept-Language: en-US,en;q=0.9"]
    else:
        cmd += ["-A", "publications-venue-refresh/1.0"]
    result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=45)
    return result.stdout if result.returncode == 0 else ""


# ── Google Scholar Metrics ───────────────────────────────────────────────────

def fetch_scholar_category(category):
    """Return [(rank, name, h5)] for one Scholar Metrics category, or []."""
    from bs4 import BeautifulSoup

    html = _curl(SCHOLAR_TOP_VENUES + category, browser=True)
    if not html:
        print(f"  {category}: no response", file=sys.stderr)
        return []
    if "captcha" in html.lower() or "unusual traffic" in html.lower():
        print(f"  {category}: Scholar returned a CAPTCHA — skipping", file=sys.stderr)
        return []

    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        name = cells[1].get_text(strip=True)
        h5 = cells[2].get_text(strip=True)
        if name and h5.isdigit():
            rows.append((len(rows) + 1, name, int(h5)))
    if not rows:
        print(f"  {category}: parsed 0 venues — Scholar's HTML may have changed",
              file=sys.stderr)
    return rows


def match_venue(hint, rows):
    """Find a venue's row in a Scholar Metrics list by name."""
    if not hint:
        return None
    target = hint.lower()
    for rank, name, h5 in rows:
        if name.lower() == target:
            return (rank, name, h5)
    best, best_score = None, 0.0
    for rank, name, h5 in rows:
        score = difflib.SequenceMatcher(None, target, name.lower()).ratio()
        if score > best_score:
            best, best_score = (rank, name, h5), score
    return best if best_score >= 0.85 else None


# ── OpenAlex ─────────────────────────────────────────────────────────────────

def fetch_openalex(search, source_id=None):
    """Return {'id', 'display_name', '2yr_mean_citedness', 'h_index'} or None."""
    if source_id:
        url = f"{OPENALEX_SOURCES}/{source_id}"
        if CONTACT:
            url += "?" + urlencode({"mailto": CONTACT})
        raw = _curl(url)
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        if not data or "id" not in data:
            return None
        results = [data]
    else:
        params = {"search": search, "per-page": "3"}
        if CONTACT:
            params["mailto"] = CONTACT
        raw = _curl(f"{OPENALEX_SOURCES}?{urlencode(params)}")
        try:
            results = (json.loads(raw) or {}).get("results") or [] if raw else []
        except json.JSONDecodeError:
            return None

    for item in results:
        # Require the name to actually resemble the query, so a failed search
        # does not silently attach another journal's impact metrics.
        if difflib.SequenceMatcher(
                None, (search or "").lower(),
                item.get("display_name", "").lower()).ratio() < 0.80:
            continue
        stats = item.get("summary_stats") or {}
        return {
            "id": item["id"].rsplit("/", 1)[-1],
            "display_name": item.get("display_name", ""),
            "2yr_mean_citedness": round(stats.get("2yr_mean_citedness") or 0, 2),
            "h_index": stats.get("h_index"),
        }
    return None


# ── description generation ───────────────────────────────────────────────────

def describe(entry, metrics, categories):
    """Regenerate the CV sentence for a venue from its fresh metrics."""
    sm = metrics.get("scholar_metrics") or {}
    if sm.get("rank") and sm.get("total"):
        category_key = ((entry.get("scholar_metrics") or {}).get("category") or "")
        label = categories.get(category_key, category_key)
        noun = "conferences" if entry.get("kind") == "conference" else "venues"
        return (f"{ordinal(sm['rank'])} of {sm['total']} in {label} {noun} "
                f"by Google Scholar")
    oa = metrics.get("openalex") or {}
    if oa.get("2yr_mean_citedness"):
        kind = "Journal" if entry.get("kind") == "journal" else "Venue"
        return (f"{kind} with a 2-year mean citedness of "
                f"{oa['2yr_mean_citedness']} (OpenAlex)")
    return ""


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report changes without writing venues.yaml")
    parser.add_argument("--source", choices=("all", "scholar", "openalex"),
                        default="all")
    args = parser.parse_args()

    venues = Venues.load()
    if not venues.venues:
        print(f"No venues found in {VENUES_PATH}", file=sys.stderr)
        return 1

    # Scholar Metrics: one request per category, not per venue.
    category_rows = {}
    if args.source in ("all", "scholar"):
        print("Fetching Google Scholar Metrics rankings…")
        for category in venues.categories:
            category_rows[category] = fetch_scholar_category(category)
            print(f"  {category}: {len(category_rows[category])} venues")
            time.sleep(2.0)

    changes, unresolved = [], []
    for key, entry in venues.venues.items():
        entry = entry or {}
        metrics = {}

        sm_cfg = entry.get("scholar_metrics") or {}
        rows = category_rows.get(sm_cfg.get("category")) or []
        if rows:
            hit = match_venue(sm_cfg.get("name") or key, rows)
            if hit:
                rank, name, h5 = hit
                metrics["scholar_metrics"] = {"rank": rank, "total": len(rows),
                                              "h5_index": h5, "matched_name": name}
            else:
                unresolved.append((key, "not found in its Scholar Metrics category"))

        oa_cfg = entry.get("openalex") or {}
        if args.source in ("all", "openalex") and oa_cfg.get("search"):
            found = fetch_openalex(oa_cfg["search"], oa_cfg.get("id"))
            if found:
                metrics["openalex"] = found
                oa_cfg.setdefault("id", found["id"])
                entry["openalex"] = oa_cfg
            else:
                unresolved.append((key, "no confident OpenAlex match"))
            time.sleep(0.3)

        if not metrics:
            continue
        metrics["refreshed"] = time.strftime("%Y-%m-%d")
        entry["metrics"] = metrics

        if venues.is_manual(key):
            continue
        new_description = describe(entry, metrics, venues.categories)
        old_description = entry.get("description") or ""
        if new_description and new_description != old_description:
            changes.append((key, old_description, new_description))
            entry["description"] = new_description

    print()
    if changes:
        print(f"{len(changes)} description(s) changed:")
        for key, old, new in changes:
            print(f"  {key}\n    was: {old}\n    now: {new}")
    else:
        print("No description changes.")
    if unresolved:
        print(f"\n{len(unresolved)} venue(s) could not be refreshed:")
        for key, reason in unresolved:
            print(f"  {key}: {reason}")

    if args.dry_run:
        print("\n(dry-run: venues.yaml not written)")
        return 0

    venues.save()
    print(f"\nWrote {VENUES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
