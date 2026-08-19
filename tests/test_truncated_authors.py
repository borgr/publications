"""Fails if the bibliography credits "et al." in place of an author list.

Not a missing entry and not an unpublished one -- an entry that is complete by
every other measure, whose `author` field ends in `and others`. Google Scholar's
BibTeX export caps long lists that way, step 2 writes what Scholar gives, and
`.bst` renders the trailing `others` as a literal "et al." *inside* the printed
entry. The CV then names ten of a paper's authors and abbreviates the rest, which
reads as a mistake because it is one.

Nothing in the pipeline looked for it. Every other check asks whether an entry
exists, whether it is published, or whether it names the CV's owner -- and this
entry passes all three. `orig.bib` held one for as long as it held the paper.

The detector is the interesting part, because the obvious implementation is
wrong: a substring test for "others" reports the Llama 3 entry as truncated on
account of its co-author Evan Smothers. Names are split on " and " first, and a
whole name has to *be* the stand-in.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bib_edit import get_truncated_author_entries, merge_published
from bib_utils import name_count, truncated_credit_fields, truncates_name_list

BIB_PATH = os.path.join(ROOT, "orig.bib")


# ── the detector ─────────────────────────────────────────────────────────────

def test_a_trailing_stand_in_is_a_truncation():
    assert truncates_name_list("Doe, Jane and Roe, Rick and others")
    assert truncates_name_list("Jane Doe and et al.")
    assert truncates_name_list("Jane Doe and ...")


def test_a_surname_containing_others_is_not():
    """Evan Smothers, on the Llama 3 entry's 561 authors. A substring test fails here."""
    assert not truncates_name_list("Evan Smothers and Fei Sun")
    assert not truncates_name_list("Smothers, Evan")
    assert not truncates_name_list("Alberto Others")


def test_a_stand_in_appended_to_a_comma_form_name():
    assert truncates_name_list("Doe, John, et al.")


def test_an_empty_or_absent_field_is_not_a_truncation():
    assert not truncates_name_list("")
    assert not truncates_name_list(None)
    assert truncated_credit_fields("  title = {A Paper},\n") == []


def test_editor_lists_are_checked_too():
    content = '  editor = {Doe, Jane and others},\n'
    assert truncated_credit_fields(content) == ["editor"]


def test_a_quoted_multiline_anthology_value_is_read_whole():
    """The Anthology writes `author = "A  and\\n  B"`. A line scan sees neither end."""
    content = '    author = "Charpentier, Lucas  and\n      Choshen, Leshem  and\n      others",\n'
    assert truncated_credit_fields(content) == ["author"]


def test_name_count_does_not_count_the_stand_in():
    assert name_count("A B and C D and others") == 2
    assert name_count("A B and C D") == 2


# ── the repair ───────────────────────────────────────────────────────────────

_TRUNCATED = """@article{k,
  title={A Paper},
  author={A B and C D and others},
  journal={arXiv preprint arXiv:1},
  year={2025}
}"""


def _published(author):
    return ("@inproceedings{k,\n  title={A Paper},\n"
            f"  author={{{author}}},\n"
            "  booktitle={Proceedings of Somewhere},\n  year={2025}\n}")


def test_a_complete_source_list_replaces_a_truncated_one():
    merged = merge_published(_TRUNCATED, _published("A B and C D and E F and G H"))
    assert "and others" not in merged
    assert "G H" in merged


def test_a_shorter_source_list_never_replaces_one():
    """A source with fewer names is not a more complete record, and adopting it
    would delete credits. Refused even though the existing list is truncated."""
    merged = merge_published(_TRUNCATED, _published("A B"))
    assert "A B and C D and others" in merged


def test_a_source_list_that_is_also_truncated_is_refused():
    merged = merge_published(_TRUNCATED, _published("A B and C D and others"))
    assert "A B and C D and others" in merged


def test_a_complete_author_list_is_still_never_overwritten():
    """The curation rule this exception is carved out of: when the existing list
    is complete, a source's differing list does not replace it. `pretitle` macros
    and hand-fixed accents live in these entries."""
    curated = _TRUNCATED.replace("A B and C D and others", "A B and C D")
    merged = merge_published(curated, _published("Wrong, Person and Other, Someone"))
    assert "author={A B and C D}" in merged
    assert "Wrong" not in merged


# ── the repo's own bibliography ──────────────────────────────────────────────

def test_orig_bib_has_no_truncated_author_lists():
    with open(BIB_PATH, encoding="utf-8") as fh:
        offenders = get_truncated_author_entries(fh.read())
    assert not offenders, (
        f"{len(offenders)} entry/entries credit \"et al.\" instead of an author "
        f"list. Step 3 retries these against the published sources every run; "
        f"what survives needs the authors pasted in from the publisher's own "
        f"BibTeX:\n    " + "\n    ".join(
            f"{e['item_name']} — {truncated_credit_fields(e['content'])}"
            for e in offenders))


# ── the wiring, end to end ───────────────────────────────────────────────────

_PUBLISHED_BUT_TRUNCATED = """@inproceedings{team2025paper,
  title={A Big Collaboration},
  author={Doe, Jane and Roe, Rick and others},
  booktitle={Proceedings of Somewhere},
  publisher={ACM},
  doi={10.1/x},
  pages={1--9},
  year={2025}
}
"""

_FULL = """@inproceedings{team2025paper,
  title={A Big Collaboration},
  author={Doe, Jane and Roe, Rick and Ng, Sam and Ito, Mei},
  booktitle={Proceedings of Somewhere},
  publisher={ACM},
  doi={10.1/x},
  pages={1--9},
  year={2025}
}"""


def test_step_3_repairs_a_published_entry_in_place(tmp_path, monkeypatch, capsys):
    """The whole path, because the wiring is where this silently does nothing.

    This entry is published, has a DOI, a publisher, pages and a venue. The
    preprint-to-published gate does not select it and never re-resolves it, so
    without the truncation rung its "and others" is permanent. Asserted through
    `main` rather than the pieces: each piece already works in isolation, and did,
    while nothing reached them.
    """
    import resolve_arxiv

    bib = tmp_path / "orig.bib"
    bib.write_text(_PUBLISHED_BUT_TRUNCATED, encoding="utf-8")

    monkeypatch.setattr(resolve_arxiv, "resolve",
                        lambda title, arxiv_id, key, content: (_FULL, "DBLP"))
    monkeypatch.setattr(resolve_arxiv, "prefetch_s2_by_arxiv", lambda ids: 0)
    monkeypatch.setattr(resolve_arxiv, "save_attempts", lambda a: None)

    resolve_arxiv.main(["--bib", str(bib), "--output", str(tmp_path / "out.bib"),
                        "--skip-missing", "--in-place"])

    after = bib.read_text(encoding="utf-8")
    assert "and others" not in after, capsys.readouterr().out
    assert "Ng, Sam" in after and "Ito, Mei" in after
    # The venue was already right and must not have been disturbed.
    assert "Proceedings of Somewhere" in after


def test_step_3_leaves_a_complete_entry_alone(tmp_path, monkeypatch):
    """The same run, with nothing to fix, must not rewrite the file."""
    import resolve_arxiv

    bib = tmp_path / "orig.bib"
    bib.write_text(_FULL + "\n", encoding="utf-8")
    before = bib.read_text(encoding="utf-8")

    monkeypatch.setattr(resolve_arxiv, "resolve",
                        lambda title, arxiv_id, key, content: (_FULL, "DBLP"))
    monkeypatch.setattr(resolve_arxiv, "prefetch_s2_by_arxiv", lambda ids: 0)
    monkeypatch.setattr(resolve_arxiv, "save_attempts", lambda a: None)

    resolve_arxiv.main(["--bib", str(bib), "--output", str(tmp_path / "out.bib"),
                        "--skip-missing", "--in-place"])

    assert bib.read_text(encoding="utf-8") == before
