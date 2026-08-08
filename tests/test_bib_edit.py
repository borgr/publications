"""Editing orig.bib: keys, entry surgery, and the in-place update.

orig.bib is hand-curated and is the pipeline's only copy, so every test here is
about a write that must not happen, or must not happen wholesale. Several are
regressions from real runs: replacing an entry outright deleted seven `pretitle`
macros invisibly, and `re.sub` treating a BibTeX replacement as a template killed
a run outright after every lookup had already been made.

No network, and nothing stubbed -- none of this code makes a request.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bib_edit
from bib_edit import (
    _PUBLISHED_SOURCES,
    _get_arxiv_id,
    _is_arxiv,
    _replace_key,
    gen_key,
    get_missing_bib_entries,
    merge_published,
    placeholder_key,
    update_bib_inplace,
)
from bib_utils import extract_field, parse_bibtex

PUBLISHED = '''@inproceedings{doe2024paper,
  title = {A Published Paper},
  booktitle = {ACL},
  year = {2024}
}'''

PREPRINT = '''@misc{doe2024preprint,
  title = {A Preprint},
  eprint = {2401.00001},
  archivePrefix = {arXiv},
  year = {2024}
}'''

# ── which entries are candidates for upgrading ───────────────────────────────

def test_published_inproceedings_is_not_treated_as_a_preprint():
    (entry,) = parse_bibtex(PUBLISHED)
    assert not _is_arxiv(entry)


def test_preprint_is_detected():
    (entry,) = parse_bibtex(PREPRINT)
    assert _is_arxiv(entry)
    assert _get_arxiv_id(entry) == "2401.00001"


def test_arxiv_id_is_found_in_a_url_when_no_eprint_field():
    (entry,) = parse_bibtex(
        '@misc{k, title = {T}, url = {https://arxiv.org/abs/2312.09876}}')
    assert _get_arxiv_id(entry) == "2312.09876"

# ── in-place rewriting of orig.bib ───────────────────────────────────────────

def test_only_published_sources_replace_an_existing_entry():
    """An arXiv-sourced result must never overwrite what is already there."""
    replacement = '@misc{doe2024preprint, title = {Should Not Land}}'
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "arXiv (export API)")], [])
    assert replaced == 0
    assert "Should Not Land" not in text


def test_published_source_upgrades_the_entry():
    """The venue moves across; the curated title stays. See merge_published."""
    replacement = ('@inproceedings{doe2024preprint, title = {Now Published}, '
                   'booktitle = {ACL 2024}, pages = {1--9}}')
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DBLP")], [])
    assert replaced == 1
    assert "ACL 2024" in text and "1--9" in text
    assert text.lstrip().startswith("@inproceedings")
    assert "A Preprint" in text        # the title is not the source's to change
    assert "Now Published" not in text


def test_appending_skips_a_key_that_already_exists():
    text, _, appended = update_bib_inplace(
        PUBLISHED, [], [("doe2024paper", "@misc{doe2024paper, title = {Dup}}")])
    assert appended == 0
    assert "Dup" not in text


def test_appended_entry_is_parseable_afterwards():
    text, _, appended = update_bib_inplace(
        PUBLISHED, [], [("new2024key", '@misc{new2024key, title = {Fresh}}')])
    assert appended == 1
    assert {e["item_name"] for e in parse_bibtex(text)} == {"doe2024paper", "new2024key"}


# ── key generation ───────────────────────────────────────────────────────────

def test_gen_key_shape():
    assert gen_key("Yadav, Prateek and Choshen, Leshem", "2023",
                   "TIES-Merging: Resolving Interference") == "yadav2023ties"


def test_gen_key_skips_stopwords():
    assert gen_key("Doe, Jane", "2024", "On the Weaknesses of RL") == "doe2024weaknesses"


def test_placeholder_key_is_deterministic():
    assert placeholder_key("2024", "A Paper With No Authors") == \
           placeholder_key("2024", "A Paper With No Authors")

# ── the unified missing-entry query ──────────────────────────────────────────

def _df(rows):
    return pd.DataFrame(rows)


def test_missing_entry_preserves_a_hand_assigned_key():
    """A key typed into the table but not yet resolved must not be regenerated."""
    df = _df([{"Name": "A Paper", "Bib": "mykey2024", "Authors": "Doe, Jane",
               "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"] == "mykey2024"


def test_missing_entry_generates_a_key_from_authors():
    df = _df([{"Name": "TIES-Merging: Resolving Interference", "Bib": None,
               "Authors": "Yadav, Prateek", "year": 2023}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"] == "yadav2023ties"


def test_missing_entry_without_authors_uses_a_placeholder():
    df = _df([{"Name": "An Orphan Paper", "Bib": None, "Authors": None, "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"].startswith("unknown2024")


def test_row_already_present_in_the_bib_is_not_missing():
    df = _df([{"Name": "A Published Paper", "Bib": "doe2024paper",
               "Authors": "Doe, Jane", "year": 2024}])
    assert get_missing_bib_entries(PUBLISHED, df=df) == []


def test_nan_bib_cell_is_treated_as_empty():
    """pandas turns blank cells into the string 'nan' via str(); it is not a key."""
    df = _df([{"Name": "A Paper", "Bib": float("nan"), "Authors": "Doe, Jane",
               "year": 2024}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert "nan" not in entry["item_name"]


def test_rows_without_a_name_are_ignored():
    df = _df([{"Name": None, "Bib": None, "Authors": None, "year": 2024}])
    assert get_missing_bib_entries("", df=df) == []

# ── regex replacement must never be treated as a template ────────────────────
#
# A real run died with `re.error: bad escape \i` after making every lookup,
# because re.sub parses its *replacement* for escapes and BibTeX is full of
# backslashes. All of the run's work was lost.

# The booktitle carries the backslashes too, because the venue is the field the
# transplant actually moves -- a LaTeX-safe title is no use if the venue is not.
LATEX_HEAVY = (r'@inproceedings{doe2024preprint,' "\n"
               r'  title = {Mod{\`e}les de langue: an {\it italic} study},' "\n"
               r'  author = {Rapha{\"e}l Doe and Jos{\'e} Roe},' "\n"
               r'  booktitle = {Actes de la Conf{\'e}rence: an {\it italic} venue},' "\n"
               r'  year = {2024}' "\n"
               r'}')


def test_replacement_containing_latex_escapes_does_not_raise():
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", LATEX_HEAVY, "DBLP")], [])
    assert replaced == 1
    assert r"Conf{\'e}rence" in text
    assert r'{\it italic} venue' in text


def test_appending_an_entry_with_latex_escapes_does_not_raise():
    entry = LATEX_HEAVY.replace("doe2024preprint", "new2024latex")
    text, _, appended = update_bib_inplace(PUBLISHED, [], [("new2024latex", entry)])
    assert appended == 1
    assert {e["item_name"] for e in parse_bibtex(text)} == {"doe2024paper", "new2024latex"}


def test_replace_key_handles_latex_escapes():
    out = _replace_key(LATEX_HEAVY, "renamed2024key")
    assert out.startswith("@inproceedings{renamed2024key,")
    assert r'{\`e}' in out


def test_replace_key_preserves_a_key_with_punctuation():
    src = '@article{DBLP:journals/corr/abs-2404-1, title = {T}}'
    assert _replace_key(src, "DBLP:conf/acl/X24").startswith(
        "@article{DBLP:conf/acl/X24,")


def test_a_backslash_heavy_entry_survives_a_full_round_trip():
    """The invariant: what goes in must parse back out identically."""
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", LATEX_HEAVY, "DBLP")], [])
    (entry,) = [e for e in parse_bibtex(text) if e["item_name"] == "doe2024preprint"]
    from bib_utils import extract_field, is_wellformed_entry
    assert "italic" in extract_field(entry["content"], "booktitle")
    assert is_wellformed_entry(entry["beg"] + entry["rest"],
                               expected_key="doe2024preprint")

def test_a_doi_result_may_upgrade_an_existing_entry():
    """A DOI is an exact identifier, so unlike an arXiv result it is allowed to
    write. The resolver's other identifier-only source has the same standing."""
    assert "DOI (clibib)" in _PUBLISHED_SOURCES
    replacement = ('@article{doe2024preprint, title = {Published Version}, '
                   'journal = {A Real Journal}, volume = {12}}')
    text, replaced, _ = update_bib_inplace(
        PREPRINT, [("doe2024preprint", replacement, "DOI (clibib)")], [])
    assert replaced == 1
    assert "A Real Journal" in text


# ── keys must be unique, and must never contain "nan" ────────────────────────

def test_gen_key_omits_an_unknown_year():
    """A NaN year used to be str()'d into the key: `arvivnanstop`."""
    assert gen_key("Arviv, O", "nan", "Stop Guessing When to Stop") == "arvivstop"
    assert gen_key("Arviv, O", float("nan"), "Stop Guessing") == "arvivstop"
    assert gen_key("Arviv, O", "", "Stop Guessing") == "arvivstop"
    assert "nan" not in placeholder_key("nan", "Some Title")


def test_gen_key_accepts_a_numeric_year():
    assert gen_key("Doe, J", 2024, "Some Paper") == "doe2024some"
    assert gen_key("Doe, J", "2024.0", "Some Paper") == "doe2024some"


def test_gen_key_disambiguates_a_collision_readably():
    """Two distinct 'Every Eval Ever' papers were handed the same key."""
    first = gen_key("Batzner, J", "2026", "Every Eval Ever: Toward a common language")
    second = gen_key("Batzner, J", "2026",
                     "Every Eval Ever: A Unifying Schema and Community Repository",
                     taken={first})
    assert first != second
    assert second.startswith(first), "should extend, not renumber, when it can"


def test_gen_key_falls_back_to_a_suffix_when_words_run_out():
    assert gen_key("Doe, J", "2024", "Alpha", taken={"doe2024alpha"}) == "doe2024alpha2"


def test_missing_entries_never_share_a_key():
    """The invariant: one pass must not hand two rows the same key."""
    df = _df([
        {"Name": "Every Eval Ever: Toward a common language", "Bib": None,
         "Authors": "Batzner, J", "year": 2026},
        {"Name": "Every Eval Ever: A Unifying Schema and Community Repository",
         "Bib": None, "Authors": "Batzner, J", "year": 2026},
        {"Name": "Every Eval Ever: Something Else Again", "Bib": None,
         "Authors": "Batzner, J", "year": 2026},
    ])
    keys = [e["item_name"] for e in get_missing_bib_entries("", df=df)]
    assert len(keys) == len(set(keys)) == 3, keys


def test_generated_key_never_collides_with_an_existing_bib_entry():
    df = _df([{"Name": "A Published Paper Renamed", "Bib": None,
               "Authors": "Doe, Jane", "year": 2024}])
    existing = '@inproceedings{doe2024published, title = {X}}'
    (entry,) = get_missing_bib_entries(existing, df=df)
    assert entry["item_name"] != "doe2024published"

# ── the venue transplant ──────────────────────────────────────────────────────
# Every test here is a real regression: replacing an entry wholesale silently
# deleted seven `pretitle` macros in one run, and the DBLP title reader's brace
# handling decided whether a published version was found at all.

_CURATED = """@article{doe2023thing,
    pretitle={\\COL\\META},
  title        = {The Thing: A Hand-Repaired Title},
  author       = {Doe, Jane and Roe, Richard},
  journal      = {CoRR},
  volume       = {abs/2301.00001},
  year         = {2023},
  eprint       = {2301.00001},
  archiveprefix = {arXiv}
}"""

_DBLP_PUBLISHED = """@inproceedings{doe2023thing,
  author    = {Jane Doe and Richard Roe},
  title     = {\\texttt{Thing}: {A} {DBLP} Title With {B}races},
  booktitle = {Proceedings of Something Real},
  pages     = {1--10},
  publisher = {ACL},
  year      = {2023},
  doi       = {10.0000/REAL.1}
}"""


def test_transplant_keeps_pretitle_title_and_author():
    merged = merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "pretitle={\\COL\\META}" in merged
    assert "A Hand-Repaired Title" in merged
    assert "Doe, Jane and Roe, Richard" in merged
    assert "DBLP Title" not in merged


def test_transplant_moves_the_venue_and_drops_the_preprint_one():
    merged = merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "Proceedings of Something Real" in merged
    assert "pages" in merged and "1--10" in merged
    assert "CoRR" not in merged
    assert merged.lstrip().startswith("@inproceedings")


def test_transplant_keeps_the_arxiv_id():
    """It stays true after publication, and downstream tools match on it."""
    merged = merge_published(_CURATED, _DBLP_PUBLISHED)
    assert "2301.00001" in merged


def test_transplant_output_still_parses_as_one_entry():
    from bib_utils import is_wellformed_entry
    merged = merge_published(_CURATED, _DBLP_PUBLISHED)
    assert is_wellformed_entry(merged, expected_key="doe2023thing")


def test_transplant_falls_back_to_the_original_on_unusable_input():
    assert merge_published(_CURATED, "not bibtex at all") == _CURATED


def test_update_bib_inplace_preserves_pretitle():
    """The end-to-end shape of the seven-macro loss."""
    new_text, n_replaced, _ = update_bib_inplace(
        _CURATED + "\n", [("doe2023thing", _DBLP_PUBLISHED, "DBLP")], [])
    assert n_replaced == 1
    assert "pretitle" in new_text
    assert "Proceedings of Something Real" in new_text

# ── rewriting orig.bib when the file has moved on ────────────────────────────
#
# The lookups happen over minutes, against a file the author may be editing. An
# update that cannot find what it meant to change must skip that entry, not
# guess at where it went.

def test_an_update_for_an_entry_no_longer_in_the_file_is_skipped():
    text, replaced, appended = update_bib_inplace(
        PREPRINT, [("some_other_key", PUBLISHED, "DBLP")], [])
    assert (replaced, appended) == (0, 0)
    assert text == PREPRINT


def test_an_update_that_would_change_nothing_is_not_counted():
    """merge_published transplants a venue. If the entry already has that venue
    there is nothing to do, and reporting it as upgraded would be a lie."""
    text, replaced, _ = update_bib_inplace(
        PUBLISHED, [("doe2024paper", PUBLISHED, "DBLP")], [])
    assert replaced == 0
    assert text == PUBLISHED


def test_a_quoted_acl_style_entry_survives_the_transplant():
    """The Anthology quotes its fields. Assuming braces here is what previously
    replaced a closing quote with a brace and produced unparseable BibTeX."""
    quoted = ('@misc{doe2024preprint,\n    title = "A Preprint",\n'
              '    year = "2024",\n    volume = 12\n}')
    replacement = ('@inproceedings{doe2024preprint, title = "A Preprint", '
                   'booktitle = "ACL", year = "2024"}')
    text, replaced, _ = update_bib_inplace(
        quoted, [("doe2024preprint", replacement, "ACL Anthology")], [])
    assert replaced == 1
    (entry,) = parse_bibtex(text)
    assert entry["title"] == "A Preprint"
    assert "ACL" in entry["content"]

# ── table cells that are not what the column promises ────────────────────────

def test_a_non_numeric_paper_flag_does_not_exclude_the_row():
    """The column means "is this a paper", and only an explicit 0 says no. A note
    typed into it must not silently drop the paper from the CV."""
    df = _df([{"Name": "A Paper", "Bib": None, "Authors": "Doe, Jane",
               "year": 2024, "Paper": "yes"}])
    assert len(get_missing_bib_entries("", df=df)) == 1


def test_a_paper_flag_of_zero_excludes_the_row():
    df = _df([{"Name": "A Proceedings Volume", "Bib": None, "Authors": "Doe, Jane",
               "year": 2024, "Paper": 0}])
    assert get_missing_bib_entries("", df=df) == []


def test_a_year_that_is_not_a_number_is_kept_as_written():
    """"to appear", "2024a", "in press": all real cells. The key generator has to
    take them rather than raise in the middle of a run."""
    df = _df([{"Name": "A Forthcoming Paper", "Bib": None, "Authors": "Doe, Jane",
               "year": "to appear"}])
    (entry,) = get_missing_bib_entries("", df=df)
    assert entry["item_name"].startswith("doe")
    assert "nan" not in entry["item_name"]


# ── writing back ─────────────────────────────────────────────────────────────

_EXISTING = """@misc{k1,
  title = {A Paper},
  eprint = {2401.00001},
  archivePrefix = {arXiv},
  year = {2024}
}
"""


def test_bibtex_that_does_not_parse_back_is_refused(capsys):
    """An unbalanced brace written verbatim takes the remainder of the file with
    it, so one bad response would cost every entry after it."""
    text, replaced, _ = update_bib_inplace(
        _EXISTING, [("k1", "@article{k1, title = {Unbalanced {oops}", "DBLP")], [])
    assert replaced == 0
    assert text == _EXISTING
    assert "[rejected]" in capsys.readouterr().err


def test_a_lower_ranked_result_never_replaces_a_better_entry(capsys):
    """The source label says where a result came from, not how good it is: DBLP
    can return a workshop @misc for a paper whose existing entry is the
    @inproceedings version of record."""
    existing = "@inproceedings{k1,\n  title = {A Paper},\n  booktitle = {ACL},\n" \
               "  year = {2024}\n}\n"
    _text, replaced, _ = update_bib_inplace(
        existing,
        [("k1", "@misc{k1,\n  title = {A Paper},\n  year = {2024}\n}", "DBLP")], [])
    assert replaced == 0
    assert "[keep existing]" in capsys.readouterr().err


def test_a_key_the_file_does_not_have_is_skipped():
    _text, replaced, _ = update_bib_inplace(
        _EXISTING,
        [("absent", "@article{absent,\n  title = {X},\n  journal = {J}\n}", "DBLP")], [])
    assert replaced == 0


def test_a_result_identical_to_what_is_there_is_not_a_change():
    """Otherwise every run rewrites orig.bib, and the diff that should show real
    upgrades shows noise instead."""
    _text, replaced, _ = update_bib_inplace(
        _EXISTING, [("k1", _EXISTING.strip(), "DBLP")], [])
    assert replaced == 0


def test_an_appended_entry_that_does_not_parse_is_refused(capsys):
    text, _, appended = update_bib_inplace(
        _EXISTING, [], [("new", "@article{new, title = {Broken {")])
    assert appended == 0
    assert text == _EXISTING
    assert "not appended" in capsys.readouterr().err


def test_appending_and_replacing_are_counted_separately():
    text, replaced, appended = update_bib_inplace(
        _EXISTING,
        [("k1", "@inproceedings{k1,\n  title = {A Paper},\n  booktitle = {ACL},\n"
                "  year = {2024}\n}", "DBLP")],
        [("new", "@article{new,\n  title = {Another},\n  journal = {J},\n"
                 "  year = {2024}\n}")])
    assert (replaced, appended) == (1, 1)
    # extract_field, not a substring: merge_published pads field names to a
    # column, so the merged line reads `booktitle     = {ACL}`.
    assert extract_field(text, "booktitle") == "ACL"
    assert "@article{new," in text


# ── missing-entry discovery ──────────────────────────────────────────────────

def test_an_unreadable_table_is_a_warning_not_a_crash(monkeypatch, capsys):
    """The table is a spreadsheet the author has open while the pipeline runs, so
    a locked file is a normal condition rather than a reason to fail."""
    def _boom():
        raise OSError("papers.csv is locked")
    monkeypatch.setattr(bib_edit, "read_df", _boom)
    assert get_missing_bib_entries("") == []
    assert "could not read the publications table" in capsys.readouterr().err
