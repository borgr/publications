"""Paper identity: stable identifiers, and the joins that used to be fuzzy.

The problem this solves
-----------------------
Every join in this pipeline used to be a title-string comparison, and titles are
not stable across sources: BibTeX braces capitalization (`{B}aby{LM}`), Scholar
lowercases subtitles, and the author's own table is typed by hand. So the
citation join silently mismatched -- two BabyLM papers holding 490 citations
between them both reported 0, because `difflib` compared raw strings and then
one match overwrote the other with no warning.

The design
----------
Sources hand out stable identifiers, and we write them down the first time we
see them, so a fuzzy match happens at most once per paper ever:

  * Google Scholar    `citation_for_view=USER:PUBID`, scraped from the profile row
  * Semantic Scholar  `externalIds` -> {ArXiv, DOI, ACL, DBLP, CorpusId} together,
                      which is the crosswalk when ACL knows no arXiv ID and
                      vice versa
  * ACL / arXiv / DOI as they appear in the bibliography

Ownership is split deliberately. The human-edited table owns judgements (venue,
tags, whether it counts as a paper); `identity.json` owns machine-harvested IDs.
One writer each, so a refresh never fights a hand edit and neither produces
merge conflicts in the other's file.

Match tiers, most trusted first -- `MATCH_EXACT_ID`, `MATCH_NORMALIZED`,
`MATCH_FUZZY`. Anything resolved below `MATCH_EXACT_ID` is reported as needing
confirmation, and anything ambiguous is reported rather than guessed.
"""

import difflib
import json
import os
import re

from bib_utils import normalize_text

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
IDENTITY_PATH = os.path.join(FILE_DIR, "identity.json")

# Match tiers, in descending trust. Only MATCH_EXACT_ID needs no human review.
MATCH_EXACT_ID = "exact-id"
MATCH_NORMALIZED = "normalized-title"
MATCH_FUZZY = "fuzzy-title"

# Ambiguity, reported rather than resolved.
MATCH_TOO_CLOSE = "too-close-to-call"

# Fuzzy thresholds. difflib.get_close_matches' 0.6 default was far too loose --
# it happily matched different papers in the same series. But raising the bar
# alone loses real renames: this table says "Will it Blend? Weak and Manual
# Labeled Data..." where Scholar says "Will it blend? blending weak and strong
# labeled data...", which is the same paper at 0.84. So there are two bars:
# accept silently above FUZZY_CUTOFF, accept but flag for confirmation above
# FUZZY_REVIEW_CUTOFF, reject below it.
FUZZY_CUTOFF = 0.90
FUZZY_REVIEW_CUTOFF = 0.75

# A fuzzy winner must beat the runner-up by this margin. Without it, the two
# "Findings of the {second,third} BabyLM Challenge" papers are each other's
# best non-exact match and either could win.
FUZZY_MARGIN = 0.05

# Fuzzy matching needs this many normalized characters to mean anything. Exact
# normalized matching is *not* gated on length -- a table row titled
# "TextArena" normalizes to 9 characters and must still match Scholar's
# "Textarena" exactly.
MIN_FUZZY_CHARS = 20

ID_FIELDS = ("scholar_id", "arxiv", "doi", "acl", "s2", "dblp")


def normalize_title(title):
    """Normalize a title for comparison across sources.

    Strips everything that differs between sources by convention rather than by
    meaning: case, punctuation, whitespace, and BibTeX's capitalization braces.
    """
    return normalize_text(title)


def title_stem(title):
    """Normalize, dropping a short prefix before a colon.

    Scholar renders series and project prefixes inconsistently ("Project
    Debater: An Autonomous Debating System" vs "An autonomous debating system"),
    so the longer side of a colon is the more reliable comparison key. Only used
    as a fallback below normalized matching, never for an exact claim.
    """
    t = (title or "").strip()
    if ":" in t:
        t = max((p.strip() for p in t.split(":", 1)), key=len)
    return normalize_text(t)


def _synthetic_key(title):
    """Key for a paper that has no BibTeX key yet (step 3 has not resolved it)."""
    return "~title:" + normalize_title(title)


class IdentityStore:
    """Machine-harvested identifiers per paper, persisted to identity.json.

    Keyed by BibTeX key where one exists, and by a synthetic title key until
    step 3 assigns one. `titles` records every spelling of a title we have seen
    for the paper, which is what lets a renamed title still resolve.
    """

    def __init__(self, records=None):
        self.records = records or {}

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path=IDENTITY_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(data.get("records", {}))

    def save(self, path=IDENTITY_PATH):
        """Persist atomically, sorted, so the file diffs cleanly in git."""
        tmp = path + ".tmp"
        payload = {
            "_comment": "Machine-harvested paper identifiers. Regenerated by "
                        "update.py; safe to delete (it will be rebuilt, at the "
                        "cost of re-resolving). Hand edits belong in the "
                        "publications table, not here.",
            "records": {k: self.records[k] for k in sorted(self.records)},
        }
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)

    # ── mutation ─────────────────────────────────────────────────────────────

    def record(self, key, title=None, **ids):
        """Merge identifiers (and a seen title spelling) into a paper's record.

        Never overwrites a stored identifier with a different value -- a
        conflict means two papers were conflated, which is reported by
        `conflicts()` rather than silently resolved.
        """
        rec = self.records.setdefault(key, {})
        for field, value in ids.items():
            if field not in ID_FIELDS or not value:
                continue
            value = str(value).strip()
            existing = rec.get(field)
            if existing and existing != value:
                rec.setdefault("_conflicts", {}).setdefault(field, [])
                if value not in rec["_conflicts"][field]:
                    rec["_conflicts"][field].append(value)
                continue
            rec[field] = value
        if title:
            titles = rec.setdefault("titles", [])
            norm = normalize_title(title)
            if norm and norm not in titles:
                titles.append(norm)
        return rec

    def rekey(self, old_key, new_key):
        """Move a synthetic-key record onto its real BibTeX key once assigned."""
        if old_key == new_key or old_key not in self.records:
            return
        merged = self.records.pop(old_key)
        target = self.records.setdefault(new_key, {})
        for field, value in merged.items():
            if field == "titles":
                seen = target.setdefault("titles", [])
                seen.extend(t for t in value if t not in seen)
            else:
                target.setdefault(field, value)

    def conflicts(self):
        """Return [(key, field, [values])] where a source disagreed with us."""
        out = []
        for key, rec in sorted(self.records.items()):
            for field, values in (rec.get("_conflicts") or {}).items():
                out.append((key, field, [rec.get(field)] + list(values)))
        return out

    # ── lookup ───────────────────────────────────────────────────────────────

    def index(self, field):
        """Return {identifier_value: paper_key} for one ID field.

        First key wins, deterministically by sort order. A value claimed by more
        than one paper is a data problem, not something to resolve here -- see
        `shared_identifiers()`.
        """
        out = {}
        for key in sorted(self.records):
            value = (self.records[key] or {}).get(field)
            if value and value not in out:
                out[value] = key
        return out

    def shared_identifiers(self):
        """Return [(field, value, [keys])] where one identifier names two papers.

        Two bib keys carrying the same Scholar ID or DOI means the same paper is
        recorded twice -- usually a duplicate table row. Silently collapsing them
        (which `index` has to do) would hide it, so it is surfaced instead.
        """
        out = []
        for field in ID_FIELDS:
            owners = {}
            for key in sorted(self.records):
                value = (self.records[key] or {}).get(field)
                if value:
                    owners.setdefault(value, []).append(key)
            out.extend((field, value, keys)
                       for value, keys in sorted(owners.items()) if len(keys) > 1)
        return out

    def title_index(self):
        """Return {normalized_title: paper_key} over every recorded spelling."""
        out = {}
        for key, rec in self.records.items():
            for norm in rec.get("titles", []):
                out.setdefault(norm, key)
        return out


# ── the citation join ────────────────────────────────────────────────────────

class JoinResult:
    """Outcome of joining an external source onto the publications table.

    `matched` maps source-table row name -> value. `needs_review` and
    `unmatched` are what the worklist reports; nothing here is silently dropped.
    """

    def __init__(self):
        self.matched = {}
        self.method = {}
        # row_name -> the incoming row that won it. Needed to bind identifiers:
        # for a fuzzy match the two titles differ by definition, so the table
        # name alone cannot find its way back to the source record.
        self.source = {}
        self.needs_review = []   # (row_name, incoming_title, tier, score)
        # Several records for one paper, summed. Informational, not an action.
        self.aggregated = []     # (row_name, [(title, tier, score), ...], total)
        # Several records that are NOT the same paper landing on one row. Real.
        self.ambiguous = []      # (row_name, [(title, tier, score), ...], total)
        # One record that could equally be two different rows. Reported, never guessed.
        self.too_close = []      # (incoming_title, value, score)
        self.unmatched = []      # (incoming_title, value)

    def tier_counts(self):
        counts = {}
        for tier in self.method.values():
            counts[tier] = counts.get(tier, 0) + 1
        return counts


def _candidate_key(row_names_by_norm, stems_by_norm, title):
    """Resolve one incoming title to a row name.

    Returns (row_name, tier, score). `tier` is None when nothing matched, and
    MATCH_TOO_CLOSE (with row_name None) when two rows are indistinguishable --
    the caller reports that rather than picking one.
    """
    norm = normalize_title(title)
    if not norm:
        return None, None, 0.0

    # Exact on the normalized title. Length-independent: this is an identity
    # claim, not a similarity guess.
    if norm in row_names_by_norm:
        return row_names_by_norm[norm], MATCH_NORMALIZED, 1.0

    # Exact on the longer side of a colon, for series/project prefixes that one
    # source includes and the other drops.
    stem = title_stem(title)
    if stem and stem in stems_by_norm:
        return stems_by_norm[stem], MATCH_NORMALIZED, 1.0

    if len(norm) < MIN_FUZZY_CHARS:
        return None, None, 0.0

    # Sorted by (score, name) so ties break deterministically rather than on
    # dict iteration order.
    scored = sorted(
        ((difflib.SequenceMatcher(None, norm, cand_norm).ratio(), name)
         for cand_norm, name in row_names_by_norm.items()),
        reverse=True,
    )
    if not scored:
        return None, None, 0.0
    best_score, best_name = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < FUZZY_REVIEW_CUTOFF:
        return None, None, best_score
    if best_score - runner_up < FUZZY_MARGIN:
        return None, MATCH_TOO_CLOSE, best_score
    return best_name, MATCH_FUZZY, best_score


def join_citations(citation_rows, row_names, store=None):
    """Join scraped citation counts onto publications-table row names.

    `citation_rows` are dicts with at least `title` and `citations`, optionally
    `scholar_id`. `row_names` are the table's paper names.

    Tiers are tried in descending trust and a stronger tier always wins, so the
    result does not depend on input order -- unlike the previous implementation,
    where the last writer won and one BabyLM paper's count overwrote another's.
    Two incoming rows landing on one table row at the same tier is reported as
    ambiguous instead of being resolved arbitrarily.
    """
    store = store or IdentityStore()
    result = JoinResult()

    # Sorted, not set-ordered: when two table rows are duplicates of each other
    # only one can win, and which one must not depend on PYTHONHASHSEED.
    row_names = sorted({str(n).strip() for n in row_names if str(n).strip()})
    names_by_norm, stems_by_norm = _title_indexes(row_names)

    # scholar_id -> the table row it was previously bound to
    scholar_to_key = store.index("scholar_id")
    key_to_name = {}
    title_idx = store.title_index()
    for norm, name in names_by_norm.items():
        key = title_idx.get(norm)
        if key:
            key_to_name[key] = name

    tier_rank = {MATCH_EXACT_ID: 3, MATCH_NORMALIZED: 2, MATCH_FUZZY: 1}
    claims = {}  # row_name -> [(tier, score, incoming_row), ...]

    for row in citation_rows:
        title = str(row.get("title") or "").strip()
        value = row.get("citations")
        scholar_id = str(row.get("scholar_id") or "").strip()

        name = tier = None
        score = 0.0
        if scholar_id and scholar_id in scholar_to_key:
            name = key_to_name.get(scholar_to_key[scholar_id])
            if name:
                tier, score = MATCH_EXACT_ID, 1.0
        if name is None:
            name, tier, score = _candidate_key(names_by_norm, stems_by_norm, title)

        if name is None:
            if tier == MATCH_TOO_CLOSE:
                result.too_close.append((title, value, score))
            result.unmatched.append((title, value))
            continue

        claims.setdefault(name, []).append((tier, score, row))

    for name, matches in claims.items():
        # Best tier and its row represent the paper; the count is the *total*
        # across every record that matched.
        matches.sort(key=lambda m: (tier_rank[m[0]], m[1]), reverse=True)
        tier, score, best_row = matches[0]

        titles = [str(m[2].get("title") or "") for m in matches]
        # Are these records all the same paper? Only then may their counts be
        # added. Two *different* papers landing on one row is a data problem, and
        # summing them would invent a number.
        same_paper = all(
            difflib.SequenceMatcher(
                None, normalize_title(t), normalize_title(titles[0])
            ).ratio() >= FUZZY_REVIEW_CUTOFF
            for t in titles[1:])

        if same_paper:
            # Scholar splits a paper across records when it has not merged the
            # preprint with the published version. Each record counts a
            # different set of citing papers, so the paper's total is their sum,
            # which is what Scholar itself shows once the versions are merged.
            present = [m[2].get("citations") for m in matches
                       if m[2].get("citations") is not None]
            # All-None stays None: "no count reported" is not a count of zero.
            result.matched[name] = sum(present) if present else None
        else:
            # Fall back to the most-trusted single record rather than inventing
            # a total across papers that are not the same.
            result.matched[name] = best_row.get("citations")

        result.method[name] = tier
        result.source[name] = best_row

        if len(matches) > 1:
            entry = (name, [(titles[i], m[0], m[1]) for i, m in enumerate(matches)],
                     result.matched[name])
            (result.aggregated if same_paper else result.ambiguous).append(entry)

        if tier == MATCH_FUZZY:
            result.needs_review.append((name, str(best_row.get("title") or ""),
                                        tier, score))

    return result


def _title_indexes(known_titles):
    """Build the normalized and colon-stem lookups `_candidate_key` needs."""
    names_by_norm, stems_by_norm = {}, {}
    for name in sorted({str(t).strip() for t in known_titles if str(t).strip()}):
        names_by_norm.setdefault(normalize_title(name), name)
        stem = title_stem(name)
        if stem:
            stems_by_norm.setdefault(stem, name)
    return names_by_norm, stems_by_norm


def classify_title(incoming, known_titles):
    """Is `incoming` one of `known_titles`? Returns (matched, tier, score).

    Deliberately the *same* decision as the citation join -- it calls
    `_candidate_key`, the join's own matcher.

    Using a stricter bar here instead was a bug: the join accepted the retitled
    "Will it blend? blending weak and strong..." as the table's "Will it Blend?
    Weak and Manual..." at 0.88, while step 2 called it new and appended a row.
    So every fuzzy-matched paper gained a duplicate row on every single run,
    each of which then made the citation join ambiguous. Five such rows appeared
    the first time this ran.

    One question ("are these the same paper?") gets one answer. Because the bar
    is permissive, every non-exact verdict is reported by the caller rather than
    applied silently -- a wrongly-skipped new paper shows up in a report, whereas
    a wrongly-added duplicate quietly corrupts the CV and the citation counts.
    """
    names_by_norm, stems_by_norm = _title_indexes(known_titles)
    return _candidate_key(names_by_norm, stems_by_norm, incoming)


def titles_match(incoming, known):
    """True if two titles are the same paper. Pairwise form of `classify_title`."""
    matched, _tier, _score = classify_title(incoming, [known])
    return matched is not None


def find_duplicate_titles(row_names):
    """Return {normalized_title: [raw names]} for table rows that collide.

    Two rows whose titles normalize identically are the same paper entered
    twice; the pipeline cannot tell which one to attach citations to.
    """
    groups = {}
    for name in row_names:
        name = str(name).strip()
        if not name:
            continue
        groups.setdefault(normalize_title(name), []).append(name)
    return {k: v for k, v in groups.items() if len(v) > 1}


_ARXIV_RE = re.compile(r'(?:arxiv[:/]|abs/)\s*(\d{4}\.\d{4,5})', re.IGNORECASE)
_DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s{}",]+)', re.IGNORECASE)
_ACL_RE = re.compile(r'aclanthology\.org/([A-Za-z0-9.\-]+?)(?:\.pdf|/|$)', re.IGNORECASE)


def harvest_ids_from_bibtex(bibtex):
    """Pull whatever identifiers a BibTeX entry already carries."""
    ids = {}
    m = _ARXIV_RE.search(bibtex)
    if m:
        ids["arxiv"] = m.group(1)
    m = _DOI_RE.search(bibtex)
    if m:
        ids["doi"] = m.group(1).rstrip('.,')
    m = _ACL_RE.search(bibtex)
    if m:
        ids["acl"] = m.group(1)
    return ids


def harvest_ids_from_s2(s2_data):
    """Map a Semantic Scholar `externalIds` payload onto our field names.

    This is the crosswalk: S2 returns ArXiv, DOI, ACL, DBLP and CorpusId for one
    paper in a single response, so binding any one of them binds them all.
    """
    if not s2_data:
        return {}
    ext = s2_data.get("externalIds") or {}
    mapping = {"ArXiv": "arxiv", "DOI": "doi", "ACL": "acl",
               "DBLP": "dblp", "CorpusId": "s2"}
    return {ours: str(ext[theirs]) for theirs, ours in mapping.items() if ext.get(theirs)}
