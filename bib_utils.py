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
    working; the implementation lives in table_io, which reads papers.csv.
    Validation problems are reported rather than raised,
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
_PUBLISHED_SCORE = 50   # what an entry is worth once its venue name says it is published
_PREPRINT_SCORE = 10    # ...and once its venue name says it is not

_PUBLISHED_TYPES = {"inproceedings", "incollection", "book", "inbook", "proceedings"}

# A workshop paper is a real publication and belongs in the CV. It is just not the
# version of record when the *same paper* also has a main-conference or journal
# one, which happens: "Enhancing Multilingual LLM Pretraining with Model-Based
# Data Selection" is at NeurIPS 2025 and at SwissText 2025, same three authors,
# same title. Both are @inproceedings with a booktitle, a DOI, a publisher and
# pages, so before this they scored identically and the tie went to whichever
# record the source happened to return first.
_WORKSHOP_RE = re.compile(
    r'\b(workshop|co-located with|companion (?:volume|proceedings)'
    r'|student research workshop|birds of a feather)\b', re.IGNORECASE)

# Findings is not a workshop. It is a main-track-adjacent archival venue of the
# same conference, so penalising it as one would rank a Findings paper below a
# genuine workshop paper -- backwards. It is named here only to keep the workshop
# test from ever being widened into it by accident.
_FINDINGS_RE = re.compile(r'\bfindings of the\b', re.IGNORECASE)
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
        score += _PUBLISHED_SCORE
    elif etype in _PREPRINT_TYPES:
        score += 0
    elif etype == "article":
        journal = extract_field(content, "journal")
        # An @article in CoRR/arXiv is a preprint wearing a journal's clothes.
        score += 10 if (journal and _PREPRINT_JOURNAL_RE.search(journal)) else 45
    else:
        score += 20

    # Corroborating evidence of a real venue. A journal name counts the same as a
    # booktitle: an @article cannot have a booktitle, so crediting only booktitles
    # docked every journal ~20 points against a conference paper and ranked a
    # workshop above TACL. Preprint "journals" are excluded -- `journal = {ArXiv
    # preprint}` is not evidence of a venue, it is the absence of one.
    journal = extract_field(content, "journal")
    real_journal = bool(journal) and not _PREPRINT_JOURNAL_RE.search(journal)
    booktitle = extract_field(content, "booktitle")
    if booktitle or real_journal:
        score += 15

    # The entry *type* is not evidence. It is whichever type the source that
    # produced the entry happened to choose, and the sources disagree: Crossref
    # returns an AAAI paper as @article because AAAI's own metadata models its
    # proceedings as a journal, arXiv-shaped entries for published papers arrive as
    # @misc, and a hand-pasted entry is whatever someone typed. The *venue name* is
    # the reliable signal -- "Proceedings of...", "Advances in...", "Transactions
    # of..." are what they say they are, and "ArXiv preprint" likewise.
    #
    # So a venue name overrides the type rather than merely adding to it. Only in
    # the direction the name is evidence for, and only up to the published floor:
    # this corrects a mislabelled type, it does not invent a stronger venue than
    # the entry claims.
    if booktitle or real_journal:
        score = max(score, _PUBLISHED_SCORE)
    elif journal and _PREPRINT_JOURNAL_RE.search(journal):
        score = min(score, _PREPRINT_SCORE)
    if extract_field(content, "doi"):
        score += 10
    if extract_field(content, "publisher"):
        score += 5
    if extract_field(content, "pages"):
        score += 5
    if extract_field(content, "volume"):
        score += 3

    # A workshop ranks below a conference or a journal, and above a preprint.
    #
    # The magnitude is load-bearing: it has to exceed the spread of the
    # corroborating-evidence bonuses above (10 + 5 + 5 + 3 = 23), because those
    # measure how *complete a record* is, not how strong a venue is. At -12 a
    # sparse JMLR entry carrying only a volume and pages lost to a SwissText entry
    # that happened to have a DOI and a publisher too. Venue tier has to dominate
    # record completeness, or the tie-break answers a different question than the
    # one being asked.
    #
    # Still well short of the 50 an entry gets for being published at all, so a
    # workshop paper outranks every preprint. It only loses to another record *of
    # the same paper* at a stronger venue.
    venue = extract_field(content, "booktitle") or extract_field(content, "journal") or ""
    if venue and _WORKSHOP_RE.search(venue) and not _FINDINGS_RE.search(venue):
        score -= 25

    # Explicit preprint markers pull back down -- but only when nothing else in
    # the entry evidences a real venue. An arXiv id stays true after publication
    # and is worth keeping in the record, so penalising a @inproceedings that has
    # a booktitle, pages and a DOI purely for remembering its eprint ranked it
    # below an otherwise identical entry that had forgotten it.
    if not (extract_field(content, "booktitle") or real_journal
            or extract_field(content, "publisher")):
        if re.search(r'\barchiveprefix\s*=', content, re.IGNORECASE):
            score -= 8
        if re.search(r'\beprint\s*=', content, re.IGNORECASE):
            score -= 4

    # Published and preprint occupy separate bands, and nothing above can cross
    # them. Without this they overlapped: a sparse workshop entry carrying only a
    # booktitle scored 40 after the workshop penalty, while an @article with no
    # journal at all -- a preprint by every reading -- scored 48. `bib_edit`
    # compares ranks and nothing else, so it preferred the preprint.
    #
    # Clamping against `is_preprint` also makes the two functions agree by
    # construction rather than by two sets of rules being kept in step by hand,
    # which is the maintenance cost that would eventually be paid in a wrong CV.
    if is_preprint(entry):
        return min(score, _PUBLISHED_SCORE - 1)
    return max(score, _PUBLISHED_SCORE)


def is_preprint(entry):
    """True if the entry describes a preprint rather than a published version.

    Venue name before entry type, for the reason `publication_rank` gives: the type
    is whichever one the source chose, and the sources disagree. Reading the type
    first made this function contradict `publication_rank` on the same entry -- an
    @inproceedings whose only venue was "ArXiv preprint" was a preprint by rank and
    not a preprint here, and the two are used side by side in `dedupe_entries`.
    """
    etype = str(entry.get("type", "")).lower()
    content = entry.get("content", "") or ""
    journal = extract_field(content, "journal")
    if extract_field(content, "booktitle"):
        return False
    if journal and _PREPRINT_JOURNAL_RE.search(journal):
        return True
    if etype in _PUBLISHED_TYPES:
        return False
    if etype == "article":
        return not journal
    if etype in _PREPRINT_TYPES:
        return True
    return bool(re.search(r'\b(archiveprefix|eprint)\s*=', content, re.IGNORECASE))


def surname(full_name):
    """The last whitespace-separated word of a name, normalized.

    Surname rather than the whole name, because the same person appears as
    "Leshem Choshen", "L. Choshen" and "Choshen, Leshem" across DBLP, the ACL
    Anthology and hand-written entries, and only the surname survives all three.
    """
    parts = [p for p in re.split(r'[\s,]+', str(full_name).strip()) if p]
    return normalize_text(parts[-1] if parts else "")


def lists_author(content, full_name):
    """False if the entry credits a list of people that this person is not among.

    The check a mis-resolution fails. Title similarity alone accepted an ISAIM
    invited talk by one of a Nature paper's twenty co-authors as that paper's
    published version, because "An autonomous debating system" is a substring of
    "Project Debater - an autonomous debating system" -- and nothing anywhere
    asked whether the entry it settled on lists the person whose CV this is.

    `editor` counts, because proceedings volumes the author edited carry no author
    field. An entry with neither field is accepted: that is a stub with no credits
    at all, which is `build_bib`'s missing-field problem and a different report --
    this one is about being contradicted, not about being uninformative.
    """
    wanted = surname(full_name)
    if not wanted:
        return True
    fields = " ".join(extract_field(content, f) or "" for f in ("author", "editor"))
    return not fields.strip() or wanted in normalize_text(fields)


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
