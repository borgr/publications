"""Editing orig.bib: keys, entry surgery, and the in-place update.

The counterpart to bib_utils, which only reads. Everything here decides what a
BibTeX entry should become and writes it, and none of it touches the network --
so a resolver source can be swapped without going near the code that edits the
bibliography, and this code can be tested without stubbing a single request.

orig.bib is hand-curated and is the pipeline's only copy, so the rules that
protect it live here:

  * only a source in `_PUBLISHED_SOURCES` may replace an existing entry, and it
    may only move the *venue* across (`merge_published`) -- title, author and
    `pretitle` are curated locally and are not a lookup's to overwrite;
  * nothing is written that does not parse back to exactly the one entry it
    claims to be, since a stray brace otherwise takes the rest of the file with
    it;
  * a generated key is disambiguated against every key already in use, because a
    collision is silent: two rows share an entry and the build reports a count
    mismatch rather than a name.
"""

import os
import re
import sys

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FILE_DIR)

from bib_utils import (
    is_wellformed_entry,
    normalize_text,
    parse_bibtex,
    publication_rank,
    read_df,
)

# ── Classifying an entry ──────────────────────────────────────────────────────

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


def _is_corr(bibtex: str) -> bool:
    compact = bibtex.lower().replace(" ", "").replace("\n", "")
    return (
        "journal={corr}" in compact
        or "eprinttype={arxiv}" in compact
        or "archiveprefix={arxiv}" in compact
        or "arxivpreprint" in compact
    )


# ── Entry text ────────────────────────────────────────────────────────────────

def _replace_key(bibtex: str, new_key: str) -> str:
    """Rewrite an entry's key. Uses a function replacement, never a template.

    `re.sub` parses its *replacement* for backreferences and escapes, so any
    interpolated text containing a backslash is a live grenade: a BibTeX entry
    with a LaTeX accent (`\\i`, `\\'e`) raises `re.error: bad escape`. A callable
    replacement is taken literally.
    """
    return re.sub(r'(@\w+\s*\{)\s*[^,\s]+\s*,',
                  lambda m: m.group(1) + new_key + ",", bibtex, count=1)


# ── In-place bib update ───────────────────────────────────────────────────────

# Sources trusted enough to *replace* an existing orig.bib entry. An arXiv-derived
# result never replaces anything -- it would downgrade a published entry back to a
# preprint. "DOI (clibib)" qualifies because a DOI is an exact identifier.
_PUBLISHED_SOURCES = {"DBLP", "ACL Anthology", "OpenReview", "DOI (clibib)",
                      "OpenAlex"}


def _year_part(year) -> str:
    """The 4-digit year for a key, or "" when it is unknown.

    A NaN year used to be str()'d straight into the key, producing
    `arvivnanstop` and `polonanstatistical`. Omitting it is both truthful and
    stable: the key stops changing once the year becomes known... which it does
    not, so the key stays put either way.
    """
    m = re.search(r'\b(19\d{2}|20\d{2}|21\d{2})\b', str(year or ""))
    return m.group(1) if m else ""


def gen_key(authors: str, year: str, title: str, taken=()) -> str:
    """Generate a short bib key from author/year/title, e.g. 'yadav2023ties'.

    `taken` is the keys already in use. Two different papers by the same author
    in the same year whose titles share a first significant word collide
    otherwise, and the collision is silent: both rows get the same key, one bib
    entry serves both, and the build reports "matched 114 entries but 115 rows
    have a Bib key". That happened to two distinct "Every Eval Ever" papers.
    """
    last = re.sub(r'[^a-z]', '', authors.split(",")[0].strip().split()[-1].lower())
    skip = {"a", "an", "the", "of", "in", "on", "for", "with", "from", "to", "and", "is", "are"}
    word = next(
        (w.lower() for w in re.split(r'\W+', title) if w.lower() not in skip and len(w) > 2),
        "paper",
    )
    word = re.sub(r'[^a-z0-9]', '', word)
    base = f"{last}{_year_part(year)}{word}"
    return _disambiguate(base, taken, extras=[
        re.sub(r'[^a-z0-9]', '', w.lower()) for w in re.split(r'\W+', title)
        if w.lower() not in skip and len(w) > 2][1:])


def split_entry(bibtex: str) -> tuple:
    """(entry_type, key, [(field, raw_value, raw_source)]).

    `raw_value` keeps its braces or quotes; `raw_source` is the field exactly as
    it was written, leading whitespace and all, so a field nothing touches is
    emitted back byte-for-byte rather than reformatted.
    """
    m = re.match(r'\s*@(\w+)\s*\{\s*([^,]+),', bibtex)
    if not m:
        return "", "", []
    i, fields = m.end(), []
    while i < len(bibtex):
        fm = re.compile(r'\s*([A-Za-z][\w-]*)\s*=\s*').match(bibtex, i)
        if not fm:
            break
        i = start = fm.end()
        if bibtex[i] == "{":
            depth = 0
            while i < len(bibtex):
                if bibtex[i] == "{":
                    depth += 1
                elif bibtex[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        elif bibtex[i] == '"':
            i = bibtex.find('"', i + 1) + 1 or len(bibtex)
        else:
            while i < len(bibtex) and bibtex[i] not in ",}\n":
                i += 1
        fields.append((fm.group(1).lower(), bibtex[start:i], bibtex[fm.start():i]))
        j = bibtex.find(",", i)
        if j < 0:
            break
        i = j + 1
    return m.group(1), m.group(2).strip(), fields


# The venue is what resolving an entry is *for*, and it is the only thing that
# moves. Everything else belongs to the bibliography: `pretitle` is a private
# categorization macro that exists in no external record, and `title`/`author`
# are hand-repaired here -- DBLP's Holmes title is
# `\texttt{Holmes} {\unicode{8981}} ...`, which does not compile and is not what
# the paper is called. `eprint`/`archiveprefix` stay too: the arXiv id remains
# true after publication, and downstream tools match on it.
_VENUE_FIELDS = ("journal", "booktitle", "volume", "number", "pages", "publisher",
                 "series", "editor", "address", "month", "year", "doi", "url",
                 "timestamp", "biburl", "bibsource")
# DBLP's own bookkeeping. Still updated, but a new `pages` belongs next to
# `volume`, not trailing after the record's provenance.
_BOOKKEEPING = ("timestamp", "biburl", "bibsource")
_CORR_VENUE = re.compile(r'^\{?\s*(corr\b|abs/|arxiv)', re.I)


def merge_published(old_bib: str, new_bib: str) -> str:
    """Move the venue across; leave every other field exactly as it was written.

    Replacing the whole entry -- which is what this used to do -- silently
    deleted every hand edit in it. It cost seven `pretitle` macros in one run,
    invisibly: the CV still built, the affected papers just lost the tags that
    place them. Rebuilding the whole entry is nearly as bad in practice, since it
    re-indents forty lines to change three and a bibliography is reviewed by hand.
    """
    _otype, key, old_fields = split_entry(old_bib)
    ntype, _nkey, new_fields = split_entry(new_bib)
    if not ntype or not new_fields or not old_fields:
        return old_bib
    new = {f: v for f, v, _ in new_fields if f in _VENUE_FIELDS}
    cols = [len(f) + len(s) for f, s in
            re.findall(r'\n\s*([A-Za-z][\w-]*)( *)=', old_bib)]
    col = max(set(cols), key=cols.count) if cols else 0     # the column most lines use
    col = max(col, max(len(f) for f in list(new) + [f for f, _, _ in old_fields]) + 1)

    def fmt(field, value):
        return f"\n  {field.ljust(col)}= {value}"

    out, seen, last_venue = [], set(), -1
    for f, v, raw in old_fields:
        if f in new:
            out.append(fmt(f, new[f]) if new[f].strip() != v.strip() else raw)
            seen.add(f)
        elif f == "journal" and "booktitle" in new:
            continue      # a paper is not both a journal article and a proceedings paper
        elif f in ("journal", "volume") and _CORR_VENUE.match(v.strip()):
            continue      # the preprint venue, with nothing in the record to replace it
        else:
            out.append(raw)
        if f in _VENUE_FIELDS and f not in _BOOKKEEPING:
            last_venue = len(out) - 1
    # Venue fields the entry did not have -- pages and volume, usually -- go next
    # to the ones it did, not at the end after DBLP's bookkeeping.
    add = [fmt(f, v) for f, v, _ in new_fields if f in _VENUE_FIELDS and f not in seen]
    at = last_venue + 1 if last_venue >= 0 else len(out)
    out[at:at] = add
    merged = f"@{ntype}{{{key}," + ",".join(out) + "\n}"
    # Never hand back something that does not parse: a field this could not read
    # would otherwise be written out malformed and take the rest of the file with
    # it. Falling back to the original entry costs one upgrade, not the file.
    return merged if is_wellformed_entry(merged, expected_key=key) else old_bib


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

        # Locate the entry with the brace-counting parser rather than a regex.
        # The regex was `@\w+\{key,.*?\r?\n\}`, which stops at the first
        # line-initial `}` -- and that can be *inside* a field value. An abstract
        # containing a line that begins with `}` (legal BibTeX, and the parser
        # reads it correctly) was cut in half, the first part replaced and the
        # remainder left in orig.bib as orphaned text.
        current = {e["item_name"]: e for e in parse_bibtex(bib_text)}.get(key)
        if current is None:
            continue
        old_text = current["beg"] + current["rest"]
        start = bib_text.find(old_text)
        if start == -1:
            continue
        # Transplant the venue rather than swapping the entry. What the lookup
        # found is a venue; the title, author list and `pretitle` in the existing
        # entry are curated here and are not the source's to overwrite.
        merged = merge_published(old_text, new_bib.rstrip('\n'))
        if merged.strip() == old_text.strip():
            continue
        bib_text = bib_text[:start] + merged + bib_text[start + len(old_text):]
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


def placeholder_key(year: str, title: str, taken=()) -> str:
    """Key for a paper with no known first author, so gen_key cannot apply."""
    base = f"unknown{_year_part(year)}{normalize_text(title)[:10]}"
    return _disambiguate(base, taken, extras=[normalize_text(title)[10:24]])


def _disambiguate(base: str, taken, extras=()) -> str:
    """Return `base`, or a variant not already in `taken`.

    Extends with further title words before resorting to a numeric suffix, so a
    disambiguated key stays readable and stays stable when regenerated for the
    same paper.
    """
    taken = set(taken or ())
    if base not in taken:
        return base
    candidate = base
    for extra in extras:
        if not extra:
            continue
        candidate += extra
        if candidate not in taken:
            return candidate
    return next(f"{base}{n}" for n in range(2, 1000) if f"{base}{n}" not in taken)


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
    # Keys already spoken for, so no two rows are handed the same one. Includes
    # keys assigned earlier in this same pass.
    assigned = set(existing_keys)
    if "Bib" in df:
        assigned |= {str(v).strip() for v in df["Bib"].dropna()
                     if str(v).strip().lower() not in ("", "nan", "none")}
    missing = []
    for _, row in df.iterrows():
        name = str(row.get("Name", "") or "").strip()
        if not name:
            continue
        # A row flagged as not a paper (a proceedings volume, a patent) needs no
        # BibTeX entry, so it is not "missing" one.
        paper_flag = row.get("Paper")
        if paper_flag is not None and str(paper_flag).strip() not in ("", "nan"):
            try:
                if float(paper_flag) == 0:
                    continue
            except (TypeError, ValueError):
                pass
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
            key = gen_key(authors, year, name, taken=assigned)
        else:
            key = placeholder_key(year, name, taken=assigned)
        assigned.add(key)

        missing.append({"item_name": key, "title": name, "authors": authors,
                        "year": year, "content": ""})
    return missing
