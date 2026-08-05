"""Load venues.yaml, the single source of venue metadata.

Replaces the JOURNALS / CONFERENCES / VENUE_DESCRIPTIONS dicts that used to be
hardcoded in build_bib.py. Behaviour is deliberately identical -- the same keys
map to the same descriptions -- but the data is now editable without touching
code, diffable when it changes, and refreshable from the sources the numbers
actually came from (see scripts/refresh_venues.py).
"""

import os

import yaml

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
VENUES_PATH = os.path.join(FILE_DIR, "venues.yaml")


class Venues:
    """Venue keys, kinds, descriptions and name aliases."""

    def __init__(self, data):
        self.data = data or {}
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

    def is_manual(self, key):
        return bool((self.venues.get(key) or {}).get("manual"))
