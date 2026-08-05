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

import pandas as pd

FILE_DIR = os.path.dirname(os.path.abspath(__file__))

# @comment/@string/@preamble are not bibliography records and carry no key.
_NON_ENTRY_TYPES = {"comment", "string", "preamble"}

_ENTRY_START_RE = re.compile(r'@(\w+)\s*\{\s*([^,\s{}]+)\s*,', re.MULTILINE)


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
    for problem in validate(df):
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


def parse_bibtex(bib_string):
    """Parse BibTeX into entry dicts.

    Each dict has: item_name, title, type, content, beg, rest -- where
    `beg + rest` reconstructs the entry's source text exactly, which is the
    contract build_bib.py relies on to rewrite entries without reformatting them.
    `title` is "" when the entry has no readable title.
    """
    results = []
    for m in _ENTRY_START_RE.finditer(bib_string):
        entry_type = m.group(1)
        if entry_type.lower() in _NON_ENTRY_TYPES:
            continue
        open_idx = bib_string.index('{', m.start())
        close_idx = _find_matching_brace(bib_string, open_idx)
        if close_idx == -1:
            continue  # unterminated entry; leave it alone rather than guess
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


def find_duplicate_keys(entries):
    """Return {key: count} for BibTeX keys that appear more than once."""
    counts = {}
    for e in entries:
        counts[e["item_name"]] = counts.get(e["item_name"], 0) + 1
    return {k: n for k, n in counts.items() if n > 1}
