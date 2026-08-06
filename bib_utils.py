"""Shared helpers: the source table reader, a BibTeX parser, and text normalization.

The BibTeX parser is brace-counting rather than regex-based. A regex cannot
match nested braces, so the previous `title = {(.*?)},` pattern silently failed
on two common shapes and returned the sentinel "Title not found":

  * ACL Anthology exports, which quote instead of bracing:  title = "..."
  * a title that is the last field, with no trailing comma

Both are now parsed. Nothing downstream should ever see a sentinel title: an
entry whose title cannot be read gets title == "", and `resolve()` refuses to
search on an empty title rather than querying DBLP for a placeholder string.
"""

import os
import re

FILE_DIR = os.path.dirname(os.path.abspath(__file__))

# @comment/@string/@preamble are not bibliography records and carry no key.
_NON_ENTRY_TYPES = {"comment", "string", "preamble"}

_ENTRY_START_RE = re.compile(r'@(\w+)\s*\{\s*([^,\s{}]+)\s*,', re.MULTILINE)


_reported_table_problems = set()


def read_df():
    """Load the publications table.

    Kept as a thin alias so existing callers and any external scripts keep
    working; the implementation lives in table_io, which prefers papers.csv and
    falls back to the xlsx. Validation problems are reported rather than raised,
    because a slightly untidy table should still build a CV -- the problems are
    collected into WORKLIST.md.
    """
    from table_io import read_table, validate  # imported here to avoid a cycle

    df = read_table()
    # Several modules call this in one run; report each distinct problem once so
    # the same warning does not appear three times in a run's output.
    for problem in validate(df):
        if problem not in _reported_table_problems:
            _reported_table_problems.add(problem)
            print(f"Table warning: {problem}")
    return df


def _find_matching_brace(text, open_idx):
    """Return the index of the brace closing the one at open_idx, or -1.

    Brace counting ignores braces inside double-quoted field values, so
    `title = "Findings of the {B}aby{LM} Challenge"` cannot unbalance the scan.
    """
    depth = 0
    in_quotes = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if ch == '\\':          # skip an escaped character outright
            i += 2
            continue
        if ch == '"' and depth <= 1:
            # Quotes only delimit a value at field depth, not inside {...} groups.
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def extract_field(content, field):
    """Return a BibTeX field's value, or "" if the field is absent.

    Handles both delimiters -- `field = {value}` and `field = "value"` -- and
    balances nested braces so `{Findings of the {B}aby{LM} Challenge}` is read
    whole. Outer delimiters are stripped; inner braces are preserved, because
    they carry meaning in BibTeX (`{B}` protects capitalization).
    """
    for m in re.finditer(r'\b' + re.escape(field) + r'\s*=\s*', content, re.IGNORECASE):
        pos = m.end()
        if pos >= len(content):
            continue
        if content[pos] == '{':
            close = _find_matching_brace(content, pos)
            if close == -1:
                continue
            return ' '.join(content[pos + 1:close].split())
        if content[pos] == '"':
            i = pos + 1
            while i < len(content):
                if content[i] == '\\':
                    i += 2
                    continue
                if content[i] == '"':
                    return ' '.join(content[pos + 1:i].split())
                i += 1
            continue
        # Bare value (a number, or a @string macro name): read to the delimiter.
        m2 = re.match(r'([^,\n}]+)', content[pos:])
        if m2:
            return m2.group(1).strip()
    return ""


def find_field_span(content, field):
    """Locate a field's value. Returns (start, end, delimiter) or None.

    `start`/`end` bound the value *inside* its delimiters, and `delimiter` is
    "{" or '"' (or "" for a bare value). Lets a caller edit a field's value
    without assuming which delimiter it uses -- the assumption that cost
    `shorten_booktitle` correctness on quoted ACL Anthology entries, where it
    replaced the closing quote with a brace and produced unparseable BibTeX.
    """
    for m in re.finditer(r'\b' + re.escape(field) + r'\s*=\s*', content, re.IGNORECASE):
        pos = m.end()
        if pos >= len(content):
            continue
        if content[pos] == '{':
            close = _find_matching_brace(content, pos)
            if close == -1:
                continue
            return pos + 1, close, '{'
        if content[pos] == '"':
            i = pos + 1
            while i < len(content):
                if content[i] == '\\':
                    i += 2
                    continue
                if content[i] == '"':
                    return pos + 1, i, '"'
                i += 1
            continue
        m2 = re.match(r'([^,\n}]+)', content[pos:])
        if m2:
            return pos, pos + len(m2.group(1)), ''
    return None


def parse_bibtex(bib_string):
    """Parse BibTeX into entry dicts.

    Each dict has: item_name, title, type, content, beg, rest -- where
    `beg + rest` reconstructs the entry's source text exactly, which is the
    contract build_bib.py relies on to rewrite entries without reformatting them.
    `title` is "" when the entry has no readable title.
    """
    results = []
    pos = 0
    while True:
        m = _ENTRY_START_RE.search(bib_string, pos)
        if not m:
            break
        entry_type = m.group(1)
        open_idx = bib_string.index('{', m.start())
        close_idx = _find_matching_brace(bib_string, open_idx)
        if close_idx == -1:
            # Unterminated entry. Resume scanning just past this start rather
            # than giving up, so one malformed entry costs only itself.
            pos = m.end()
            continue
        # Resume *after* this entry, so an "@misc{...}" appearing inside an
        # abstract or a note cannot be mistaken for a bibliography record of
        # its own. Scanning the whole file for starts produced phantom entries.
        pos = close_idx + 1
        if entry_type.lower() in _NON_ENTRY_TYPES:
            continue
        beg = bib_string[m.start():m.end()]
        rest = bib_string[m.end():close_idx + 1]
        content = bib_string[m.end():close_idx]
        results.append({
            "item_name": m.group(2).strip(),
            "title": extract_field(content, "title"),
            "type": entry_type,
            "content": content,
            "beg": beg,
            "rest": rest,
        })
    return results


def normalize_text(txt):
    """Strip all non-alphanumeric characters for fuzzy title comparison.

    This already removes BibTeX's capitalization braces, so
    "Findings of the {B}aby{LM} Challenge" and "Findings of the BabyLM
    challenge" normalize to the same string -- which is why the citation join
    must compare normalized titles, not raw ones.
    """
    return re.sub(r'[\W_]+', '', str(txt).lower().strip())


# Entry types that are a published venue by definition.
_PUBLISHED_TYPES = {"inproceedings", "incollection", "book", "inbook", "proceedings"}
# Entry types that are a preprint or grey literature by definition.
_PREPRINT_TYPES = {"misc", "unpublished", "techreport"}

_PREPRINT_JOURNAL_RE = re.compile(r'\b(arxiv|corr|preprint)\b', re.IGNORECASE)


def publication_rank(entry):
    """Score how *published* a BibTeX entry is. Higher wins.

    One rule, used everywhere two candidates describe the same paper:

      * step 3, so resolving never downgrades a published entry to a preprint
      * the duplicate-row resolver, so the CV keeps the version of record
      * scripts/dedupe.py

    Before this existed the preference was implicit and partial -- `resolve()`
    preferred a DBLP published entry over a CoRR one, but two *rows* for the same
    paper were both emitted regardless of which was the version of record.
    """
    etype = str(entry.get("type", "")).lower()
    content = entry.get("content", "") or ""

    score = 0
    if etype in _PUBLISHED_TYPES:
        score += 50
    elif etype in _PREPRINT_TYPES:
        score += 0
    elif etype == "article":
        journal = extract_field(content, "journal")
        # An @article in CoRR/arXiv is a preprint wearing a journal's clothes.
        score += 10 if (journal and _PREPRINT_JOURNAL_RE.search(journal)) else 45
    else:
        score += 20

    # Corroborating evidence of a real venue.
    if extract_field(content, "booktitle"):
        score += 15
    if extract_field(content, "doi"):
        score += 10
    if extract_field(content, "publisher"):
        score += 5
    if extract_field(content, "pages"):
        score += 5
    if extract_field(content, "volume"):
        score += 3

    # Explicit preprint markers pull back down -- but only when nothing else in
    # the entry evidences a real venue. An arXiv id stays true after publication
    # and is worth keeping in the record, so penalising a @inproceedings that has
    # a booktitle, pages and a DOI purely for remembering its eprint ranked it
    # below an otherwise identical entry that had forgotten it.
    if not (extract_field(content, "booktitle") or extract_field(content, "publisher")):
        if re.search(r'\barchiveprefix\s*=', content, re.IGNORECASE):
            score -= 8
        if re.search(r'\beprint\s*=', content, re.IGNORECASE):
            score -= 4
    return score


def is_preprint(entry):
    """True if the entry describes a preprint rather than a published version."""
    etype = str(entry.get("type", "")).lower()
    content = entry.get("content", "") or ""
    if etype in _PUBLISHED_TYPES or extract_field(content, "booktitle"):
        return False
    if etype == "article":
        journal = extract_field(content, "journal")
        return bool(journal and _PREPRINT_JOURNAL_RE.search(journal)) or not journal
    if etype in _PREPRINT_TYPES:
        return True
    return bool(re.search(r'\b(archiveprefix|eprint)\s*=', content, re.IGNORECASE))


def _entry_year(entry):
    m = re.search(r'\b(1[89]\d{2}|20\d{2}|21\d{2})\b',
                  extract_field(entry.get("content", "") or "", "year"))
    return int(m.group(1)) if m else 0


def choose_published(entries):
    """Pick the entry to keep when several describe one paper. (winner, [losers]).

    Ordered by, in priority:

      1. published over preprint -- the version of record is what a CV should cite
      2. the newer year, *within* the same class. Two preprints of one paper are
         its v1 and v2, and the newer carries the current title: this repo has
         "Can You Trust Your Metric?" (2024) and "How Safe is Your Safety
         Metric?" (2025) as one arXiv ID, and keeping the 2024 one would print a
         title the authors have since replaced. Deliberately does not apply
         across classes -- a 2024 published paper still beats its 2025 preprint.
      3. publication_rank, then content length, then key, so the result is
         stable across runs rather than dependent on input order.
    """
    if not entries:
        return None, []
    ordered = sorted(
        entries,
        key=lambda e: (not is_preprint(e), _entry_year(e), publication_rank(e),
                       len(e.get("content", "")), e.get("item_name", "")),
        reverse=True,
    )
    return ordered[0], ordered[1:]


def escape_field_value(text):
    """Make a plain-text string safe as a braced BibTeX field value.

    Only for values built from structured data (OpenAlex and OpenReview JSON),
    which is plain text -- never for BibTeX copied from DBLP or the ACL Anthology,
    where a backslash or a brace is deliberate LaTeX.

    An unbalanced brace here is not cosmetic. `@article{k, title = {A { Brace}}`
    does not parse, and because the brace scan then runs past the entry's end it
    takes the rest of the file with it: one such title silently emptied a whole
    bibliography in testing.
    """
    if text is None:
        return ""
    # Backslashes are held aside while braces are escaped, so the braces in the
    # \textbackslash{} replacement do not themselves get escaped.
    placeholder = "\x00"
    out = str(text).replace("\\", placeholder)
    out = out.replace("{", r"\{").replace("}", r"\}")
    out = out.replace(placeholder, r"\textbackslash{}")
    return " ".join(out.split())


def is_wellformed_entry(bibtex, expected_key=None):
    """True if `bibtex` parses back to exactly one entry with a title.

    The backstop before anything is written to orig.bib. Every generated or
    fetched entry goes through it, so a malformed response from any source costs
    that one lookup rather than the file.
    """
    entries = parse_bibtex(bibtex or "")
    if len(entries) != 1:
        return False
    entry = entries[0]
    if not entry["item_name"] or not entry["title"]:
        return False
    if expected_key is not None and entry["item_name"] != expected_key:
        return False
    # Reconstruction must be lossless, which catches trailing garbage that the
    # parser skipped over rather than rejected.
    return (entry["beg"] + entry["rest"]).strip() == bibtex.strip()


def find_duplicate_keys(entries):
    """Return {key: count} for BibTeX keys that appear more than once."""
    counts = {}
    for e in entries:
        counts[e["item_name"]] = counts.get(e["item_name"], 0) + 1
    return {k: n for k, n in counts.items() if n > 1}
