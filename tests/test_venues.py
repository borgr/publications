"""venues.yaml loading, and the venue-description refresher's pure logic."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from refresh_venues import describe, match_venue, ordinal

import build_bib
from venues import Venues

YAML = """
scholar_metrics:
  categories:
    eng_computationallinguistics: computational linguistics
aliases:
  cloud:
    all_of: ["conference on cloud computing", "ieee"]
venues:
  acl:
    kind: conference
    description: 1st of 20 in computational linguistics conferences by Google Scholar
  tacl:
    kind: journal
    description: A journal
  colm:
    kind: conference
    manual: true
    description: Hand-written prose
"""


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "venues.yaml"
    path.write_text(YAML, encoding="utf-8")
    return Venues.load(str(path))


def test_kinds_split_journals_from_conferences(sample):
    assert sample.journals == {"tacl"}
    assert sample.conferences == {"acl", "colm"}


def test_description_lookup(sample):
    assert sample.description("acl").startswith("1st of 20")
    assert sample.description("nonexistent") == ""


def test_manual_flag(sample):
    assert sample.is_manual("colm")
    assert not sample.is_manual("acl")


def test_alias_requires_every_token(sample):
    assert sample.alias_for("ieee international conference on cloud computing") == "cloud"
    assert sample.alias_for("international conference on cloud computing") is None
    assert sample.alias_for("acl 2024") is None


def test_missing_file_loads_empty_rather_than_raising(tmp_path):
    assert Venues.load(str(tmp_path / "nope.yaml")).venues == {}


def test_round_trips(tmp_path, sample):
    path = str(tmp_path / "out.yaml")
    sample.save(path)
    assert Venues.load(path).description("acl") == sample.description("acl")


# ── the live file must stay consistent with what build_bib expects ───────────

def test_live_venues_file_loads_and_is_categorised():
    venues = Venues.load()
    assert venues.journals, "no journals configured"
    assert venues.conferences, "no conferences configured"
    assert not (venues.journals & venues.conferences), "a venue is both kinds"


def test_every_rankable_live_venue_has_a_description():
    """`kind: other` venues (blogs) have no ranking, so no description."""
    venues = Venues.load()
    missing = [k for k in venues.venues
               if not venues.description(k) and k not in venues.non_ranked]
    assert missing == []


def test_non_ranked_venue_is_known_but_undescribed():
    venues = Venues.load()
    assert venues.non_ranked, "expected at least one kind: other venue"
    for key in venues.non_ranked:
        assert venues.known(key), "must not be reported as a missing venue"
        assert not venues.description(key), "must emit no venueinf sentence"


def test_blog_post_resolves_and_is_filed_as_non_reviewed():
    import build_bib
    key = build_bib.simplify_venue("Blog Post, EvalEval Coalition, 2026")
    assert key == "blog"
    assert build_bib._categorize(key, False, False, False) == "drafts"


def test_build_bib_reads_the_yaml_not_hardcoded_dicts():
    """Regression guard: the dicts used to be literals inside build_bib.py."""
    assert build_bib.JOURNALS == Venues.load().journals
    assert "tacl" in build_bib.JOURNALS
    assert "acl" in build_bib.CONFERENCES


def test_cloud_alias_still_resolves_through_build_bib():
    assert build_bib.simplify_venue(
        "IEEE Transactions on Conference on Cloud Computing") == "cloud"


def test_simplify_venue_truncates_at_the_first_delimiter():
    assert build_bib.simplify_venue("ACL 2024") == "acl"
    assert build_bib.simplify_venue("EMNLP-Findings") == "emnlp"


# ── refresher logic ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (5, "5th"),
    (11, "11th"), (12, "12th"), (13, "13th"), (19, "19th"), (20, "20th"),
    (21, "21st"), (22, "22nd"), (23, "23rd"),
    (101, "101st"), (111, "111th"), (112, "112th"), (113, "113th"),
])
def test_ordinal(n, expected):
    """The old data said '1th of 20', so this is worth pinning."""
    assert ordinal(n) == expected


def test_describe_from_a_scholar_ranking():
    entry = {"kind": "conference", "scholar_metrics": {"category": "cl"}}
    metrics = {"scholar_metrics": {"rank": 2, "total": 20}}
    assert describe(entry, metrics, {"cl": "computational linguistics"}) == (
        "2nd of 20 in computational linguistics conferences by Google Scholar")


def test_describe_falls_back_to_openalex():
    entry = {"kind": "journal"}
    metrics = {"openalex": {"2yr_mean_citedness": 12.48}}
    assert describe(entry, metrics, {}) == (
        "Journal with a 2-year mean citedness of 12.48 (OpenAlex)")


def test_describe_returns_empty_with_no_metrics():
    assert describe({"kind": "conference"}, {}, {}) == ""


ROWS = [(1, "Meeting of the Association for Computational Linguistics (ACL)", 236),
        (2, "Conference on Empirical Methods in Natural Language Processing (EMNLP)", 218)]


def test_match_venue_exact_name():
    assert match_venue(ROWS[0][1], ROWS) == ROWS[0]


def test_match_venue_tolerates_small_differences():
    assert match_venue("Meeting of the Association for Computational Linguistics(ACL)",
                       ROWS) == ROWS[0]


def test_match_venue_refuses_a_weak_match():
    """A wrong match would attach another venue's ranking to this one."""
    assert match_venue("Journal of Irreproducible Results", ROWS) is None


def test_match_venue_handles_an_empty_list():
    assert match_venue("anything", []) is None


# ── the truncation fallback's delimiters ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # A year ends the venue name, whether or not a space precedes it.
    ("ACL 2024", "acl"),
    ("CogSci2024", "cogsci"),
    ("Interspeech 2024", "interspeech"),
    # A digit *inside* a name must not end it. Splitting on the literal `2` --
    # which is what the old rule did -- turned these into one letter.
    ("K2 Workshop on Knowledge", "k2 workshop on knowledge"),
    ("H2O Symposium", "h2o symposium"),
    ("L2 Learning Workshop", "l2 learning workshop"),
    ("Web3 Conference", "web3 conference"),
    # Structural delimiters still end it.
    ("EMNLP-Findings", "emnlp"),
    ("ICLR*", "iclr"),
])
def test_venue_truncation_delimiters(raw, expected):
    assert build_bib.simplify_venue(raw) == expected


def test_every_live_venue_string_still_resolves_the_same_way():
    """Guards the truncation rule against a change that silently refiles papers."""
    from table_io import read_table
    df = read_table()
    resolved = [build_bib.simplify_venue(str(v)) for v in df["Venue"].dropna()]
    venues = Venues.load()
    known = sum(1 for v in resolved if venues.known(v))
    # 64 of the live strings resolve to a configured venue; a drop means the
    # truncation rule or a match phrase regressed.
    assert known >= 64, f"only {known} venue strings resolve to a known venue"


# ── rewriting entry text ─────────────────────────────────────────────────────

def test_shorten_booktitle_trims_a_braced_tail():
    out = build_bib.shorten_booktitle('\n  booktitle = {NeurIPS 2024 Competition Track},\n')
    assert out.strip() == "booktitle = {NeurIPS 2024},"


def test_shorten_booktitle_leaves_a_quoted_field_alone():
    """The ACL Anthology quotes its booktitles, and its full names are wanted.

    The old regex assumed a closing `}`; when it did match a quoted field it
    replaced the closing quote with a brace and produced unparseable BibTeX.
    """
    src = '\n  booktitle = "Proceedings of the 2024 Conference on EMNLP",\n'
    assert build_bib.shorten_booktitle(src) == src


def test_shorten_booktitle_output_always_still_parses():
    from bib_utils import is_wellformed_entry
    for value in ('{ACL 2024, Bangkok}', '"ACL 2024, Bangkok"',
                  '{Findings of the {ACL} 2024, Bangkok}', '{No year here}'):
        entry = ('@inproceedings{k,\n  title = {T},\n  booktitle = '
                 + value + ',\n  year = {2024}\n}')
        assert is_wellformed_entry(build_bib.shorten_booktitle(entry), "k"), value


def test_shorten_booktitle_with_no_booktitle_is_a_no_op():
    src = '\n  title = {A Paper},\n  year = {2024},\n'
    assert build_bib.shorten_booktitle(src) == src


def test_remove_pretitle_tags_handles_braces_in_the_tag():
    """`[^}]*}` stopped at the first inner brace and left the field behind."""
    src = '@x{k,\n    pretitle={\\CMD{x}},\n  title = {A Paper},\n}'
    out = build_bib.remove_pretitle_tags(src)
    assert "pretitle" not in out
    assert "title = {A Paper}" in out


def test_remove_pretitle_tags_is_a_no_op_when_absent():
    src = '@x{k,\n  title = {A Paper},\n}'
    assert build_bib.remove_pretitle_tags(src) == src


def test_the_live_bibliography_is_entirely_parseable():
    """Every entry in the generated CV bibliography must re-parse standalone."""
    import os

    from bib_utils import is_wellformed_entry, parse_bibtex
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "overleaf", "Wzmn.bib")
    if not os.path.exists(path):
        pytest.skip("overleaf submodule not checked out")
    entries = parse_bibtex(open(path).read())
    assert entries
    bad = [e["item_name"] for e in entries
           if not is_wellformed_entry(e["beg"] + e["rest"], e["item_name"])]
    assert bad == [], bad


def test_no_ranked_venue_is_mistaken_for_a_workshop():
    """A guard on the workshop demotion in `publication_rank`, which is a regex over
    venue prose -- the one heuristic left in the ranking.

    Its failure direction is deliberate: an unrecognised workshop is treated as a
    conference, so the cost is a missed demotion, which is what the code did before
    the tier existed. The dangerous direction is the other one -- widening the
    pattern until it swallows a real venue and demotes a main-conference paper
    below the workshop version of the same paper. venues.yaml is the curated list
    of venues that matter, so it is the right thing to check the pattern against,
    and it costs nothing at runtime.
    """
    from bib_utils import _FINDINGS_RE, _WORKSHOP_RE

    venues = Venues.load()
    offenders = []
    for acronym, entry in venues.venues.items():
        if (entry or {}).get("kind") not in ("conference", "journal"):
            continue
        names = list(entry.get("match") or [])
        names.append((entry.get("scholar_metrics") or {}).get("name") or "")
        for name in filter(None, names):
            if _WORKSHOP_RE.search(name) and not _FINDINGS_RE.search(name):
                offenders.append((acronym, name))
    assert not offenders, (
        f"the workshop pattern matches ranked venues, which would demote real "
        f"papers: {offenders}")
