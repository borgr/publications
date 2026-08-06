"""Load venues.yaml, the single source of venue metadata.

Replaces the JOURNALS / CONFERENCES / VENUE_DESCRIPTIONS dicts that used to be
hardcoded in build_bib.py. Behaviour is deliberately identical -- the same keys
map to the same descriptions -- but the data is now editable without touching
code, diffable when it changes, and refreshable from the sources the numbers
actually came from (see scripts/refresh_venues.py).
"""

import os
import re

import yaml

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
VENUES_PATH = os.path.join(FILE_DIR, "venues.yaml")


class Venues:
    """Venue keys, kinds, descriptions and name aliases."""

    def __init__(self, data):
        self.data = data or {}
        self._phrases = None
        self.venues = self.data.get("venues") or {}
        self.aliases = self.data.get("aliases") or {}
        self.categories = ((self.data.get("scholar_metrics") or {})
                           .get("categories") or {})

    @classmethod
    def load(cls, path=VENUES_PATH):
        try:
            with open(path) as f:
                return cls(yaml.safe_load(f))
        except FileNotFoundError:
            return cls({})

    def save(self, path=VENUES_PATH):
        """Write back, preserving key order and block style for readability."""
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(self.data, f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=100)
        os.replace(tmp, path)

    # ── the interface build_bib needs ────────────────────────────────────────

    @property
    def journals(self):
        return {k for k, v in self.venues.items() if (v or {}).get("kind") == "journal"}

    @property
    def conferences(self):
        return {k for k, v in self.venues.items() if (v or {}).get("kind") == "conference"}

    def description(self, key):
        return ((self.venues.get(key) or {}).get("description") or "")

    def known(self, key):
        return key in self.venues

    def alias_for(self, raw_lowercase):
        """Return the venue key for a raw venue string needing special handling."""
        for key, rule in self.aliases.items():
            required = (rule or {}).get("all_of") or []
            if required and all(token in raw_lowercase for token in required):
                return key
        return None

    def _match_phrases(self):
        """Build [(phrase, key)] for full-name matching, longest phrase first.

        Phrases come from each venue's explicit `match:` list and from its
        Scholar Metrics name, so the long official names are usable without
        being typed twice.
        """
        if self._phrases is not None:
            return self._phrases
        phrases = []
        for key, entry in self.venues.items():
            entry = entry or {}
            for phrase in (entry.get("match") or []):
                phrases.append((str(phrase).lower(), key))
            scholar_name = (entry.get("scholar_metrics") or {}).get("name")
            if scholar_name:
                # Drop a trailing parenthetical acronym: the long name is the
                # part that appears in a proceedings title.
                long_name = re.sub(r'\s*\([^)]*\)\s*$', '', str(scholar_name)).strip()
                if len(long_name) > 12:
                    phrases.append((long_name.lower(), key))
        # Longest first, so "nature machine intelligence" wins over "nature".
        phrases.sort(key=lambda p: -len(p[0]))
        self._phrases = phrases
        return phrases

    def match_raw(self, raw):
        """Map a raw venue string to a venue key, or "" if it cannot be placed.

        Handles what Scholar actually reports for a paper's venue -- a
        proceedings title or a citation fragment, not a short name:

            "Proceedings of the 26th Conference on Computational Natural
             Language ..., 2022"                          -> conll
            "Nature 591 (7850), 379-384, 2021"            -> nature

        Step 2 copies Scholar's text verbatim when it adds a paper, so without
        this every auto-added paper carried a venue that matched nothing, got no
        `venueinf` line, and was filed under ArXiv Articles.
        """
        if not raw:
            return ""
        low = str(raw).lower()

        alias = self.alias_for(low)
        if alias:
            return alias
        # An exact short key, which is what a hand-typed cell holds.
        stripped = low.strip().rstrip(",.")
        if stripped in self.venues:
            return stripped
        # A key followed by a year or a marker, which is how these are typed by
        # hand: "ACL2022", "CoNLL2023*", "ICLR*", "NeurIPS 2023". Deliberately
        # does NOT accept a following hyphen-plus-letter, because that is a
        # different name rather than a decoration: "Nature-inspired Computing" is
        # not Nature, and "LREC-Coling" is its own joint conference. Requiring a
        # non-letter also keeps "sem" from claiming "SemEval@EMNLP".
        for key in sorted(self.venues, key=len, reverse=True):
            if re.match(re.escape(key) + r'(?=$|[\s*^,.:;]|\d)', stripped):
                return key
        for phrase, key in self._match_phrases():
            if " " in phrase:
                if re.search(r'\b' + re.escape(phrase) + r'\b', low):
                    return key
            # A single-word phrase like "nature" is too generic to match
            # anywhere in the string: it must open it, and be followed by a
            # separator rather than more word. Otherwise "Nature-inspired
            # Computing Workshop" resolves to Nature.
            elif re.match(re.escape(phrase) + r'(?=[\s,.:;]|$)', low):
                return key
        return ""

    def is_manual(self, key):
        return bool((self.venues.get(key) or {}).get("manual"))
