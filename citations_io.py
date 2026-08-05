"""Read and write citations.csv.

Format
------
One row per paper, with the stable Scholar identifier alongside the count:

    Title,Cited by,Year,Authors,Venue,Scholar ID

The `Scholar ID` is the `citation_for_view=USER:PUBID` value from the profile
row. It is stable across renames and re-rankings, so storing it turns the
citation join from a title-similarity guess into a dictionary lookup.

Legacy format
-------------
This file used to mirror Scholar's own CSV export: three rows per paper, with
the title/count/year on the first and authors and venue on the two following
rows with empty trailing cells. That shape is still readable so an offline
rebuild (and a fork that has not re-fetched yet) keeps working; it is detected
by its narrow 3-column header. The next `fetch_citations.py` run rewrites the
file in the current format, after which the legacy branch is dead weight and
can be deleted.
"""

import csv
import os

HEADER = ["Title", "Cited by", "Year", "Authors", "Venue", "Scholar ID"]

_FIELDS = {
    "Title": "title",
    "Cited by": "citations",
    "Year": "year",
    "Authors": "authors",
    "Venue": "venue",
    "Scholar ID": "scholar_id",
}


def _to_int(value):
    """Citation counts arrive as '', '12', or '12*' (Scholar's merged marker)."""
    text = str(value or "").replace("*", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_legacy(rows):
    """Parse the historical three-rows-per-paper shape."""
    papers = []
    i = 1  # skip header
    while i < len(rows):
        r0 = rows[i] if i < len(rows) else []
        r1 = rows[i + 1] if i + 1 < len(rows) else []
        r2 = rows[i + 2] if i + 2 < len(rows) else []
        title = r0[0].strip() if r0 else ""
        if title:
            papers.append({
                "title": title,
                "citations": _to_int(r0[1] if len(r0) > 1 else ""),
                "year": (r0[2].strip() if len(r0) > 2 else ""),
                "authors": (r1[0].strip() if r1 else ""),
                "venue": (r2[0].strip() if r2 else ""),
                "scholar_id": "",
            })
        i += 3
    return papers


def read_citation_rows(path):
    """Return [{title, citations, year, authors, venue, scholar_id}].

    `citations` is an int, or None when Scholar reported no count (which is
    distinct from a count of zero and must not be conflated with it).
    """
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []

    header = [c.strip() for c in rows[0]]
    if len(header) < 4:
        return _read_legacy(rows)

    idx = {name: header.index(name) for name in header if name in _FIELDS}
    papers = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        paper = {field: "" for field in _FIELDS.values()}
        for name, pos in idx.items():
            if pos < len(row):
                paper[_FIELDS[name]] = row[pos].strip()
        paper["citations"] = _to_int(paper["citations"])
        papers.append(paper)
    return papers


def write_citation_rows(papers, path):
    """Write citations.csv atomically, so an interrupted run cannot truncate it."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)
            for p in papers:
                writer.writerow([
                    p.get("title", ""),
                    "" if p.get("citations") in (None, "") else p["citations"],
                    p.get("year", ""),
                    p.get("authors", ""),
                    p.get("venue", ""),
                    p.get("scholar_id", ""),
                ])
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
