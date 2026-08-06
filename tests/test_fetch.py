"""The Scholar scraper's failure modes.

A partial scrape is more dangerous than a failed one: the result overwrites
citations.csv, so every paper past a page that failed to load silently reports
zero citations from then on, and the CV goes out with them.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_citations as fc
from citations_io import read_citation_rows, write_citation_rows


def _rows(labels):
    return [{"title": t, "authors": "A", "venue": "V", "citations": "5",
             "year": "2024", "scholar_id": f"U:{t}"} for t in labels]


@pytest.fixture
def scholar(monkeypatch):
    """A fake three-page profile. `state` lets a test break one page."""
    state = {"empty_once": set(), "requests": []}
    pages = {0: _rows([f"p{i}" for i in range(20)]),
             20: _rows([f"p{i}" for i in range(20, 40)]),
             40: _rows([f"p{i}" for i in range(40, 50)])}

    def fetch_page(_user, start):
        state["requests"].append(start)
        if start in state["empty_once"]:
            state["empty_once"].discard(start)
            return "__EMPTY__"
        return f"__PAGE_{start}__"

    def parse_page(html):
        if html == "__EMPTY__":
            return []
        if html.startswith("__PAGE_"):
            return pages.get(int(html[len("__PAGE_"):-2]), [])
        return []

    monkeypatch.setattr(fc, "_fetch_page", fetch_page)
    monkeypatch.setattr(fc, "_parse_page", parse_page)
    monkeypatch.setattr(fc, "_check_curl", lambda: None)
    monkeypatch.setattr(fc, "_curl_get", lambda url: (200, "<html></html>"))
    monkeypatch.setattr(fc, "_parse_profile_stats",
                        lambda html: {"citations": 100, "h_index": 9})
    monkeypatch.setattr(fc.time, "sleep", lambda _s: None)
    return state


# ── paging ───────────────────────────────────────────────────────────────────

def test_full_profile_is_paged_through(scholar):
    papers, stats = fc.scrape_profile("USER", delay=0)
    assert len(papers) == 50
    assert stats == {"citations": 100, "h_index": 9}


def test_a_transient_empty_page_is_retried_not_believed(scholar):
    """Believing it truncated the profile at 20 of 50 papers, silently."""
    scholar["empty_once"].add(20)
    papers, _ = fc.scrape_profile("USER", delay=0)
    assert len(papers) == 50, "the retry must recover the page"
    assert scholar["requests"].count(20) == 2, "page 20 should be fetched twice"


def test_a_genuinely_empty_page_still_ends_paging(scholar, monkeypatch):
    """The retry must not turn the end of the profile into an infinite loop."""
    monkeypatch.setattr(fc, "_parse_page",
                        lambda html: _rows([f"p{i}" for i in range(20)])
                        if html == "__PAGE_0__" else [])
    papers, _ = fc.scrape_profile("USER", delay=0)
    assert len(papers) == 20


def test_a_short_page_ends_paging_without_an_extra_request(scholar):
    fc.scrape_profile("USER", delay=0)
    assert 60 not in scholar["requests"], "page 40 was short; no page 60 needed"


# ── the shrink guard ─────────────────────────────────────────────────────────

def _run_main(monkeypatch, tmp_path, papers, argv_extra=()):
    out = tmp_path / "citations.csv"
    monkeypatch.setattr(fc, "scrape_profile",
                        lambda user, **kw: (papers, {"citations": 1, "h_index": 1}))
    monkeypatch.setattr(sys, "argv", ["fetch_citations.py", "USER",
                                      "-o", str(out), *argv_extra])
    return out


def test_a_sharp_drop_refuses_to_overwrite(monkeypatch, tmp_path):
    """The failure this guards: 50 papers become 20, and 30 read as zero."""
    out = _run_main(monkeypatch, tmp_path, _rows([f"p{i}" for i in range(20)]))
    write_citation_rows(_rows([f"p{i}" for i in range(50)]), str(out))

    with pytest.raises(RuntimeError) as excinfo:
        fc.main()
    assert "NOT overwritten" in str(excinfo.value)
    assert len(read_citation_rows(str(out))) == 50, "the good file must survive"


def test_a_small_drop_is_allowed(monkeypatch, tmp_path):
    """One paper removed from the profile is normal, not a failed page."""
    out = _run_main(monkeypatch, tmp_path, _rows([f"p{i}" for i in range(49)]))
    write_citation_rows(_rows([f"p{i}" for i in range(50)]), str(out))
    fc.main()
    assert len(read_citation_rows(str(out))) == 49


def test_growth_is_always_allowed(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, _rows([f"p{i}" for i in range(60)]))
    write_citation_rows(_rows([f"p{i}" for i in range(50)]), str(out))
    fc.main()
    assert len(read_citation_rows(str(out))) == 60


def test_allow_shrink_overrides_the_guard(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, _rows([f"p{i}" for i in range(5)]),
                    argv_extra=("--allow-shrink",))
    write_citation_rows(_rows([f"p{i}" for i in range(50)]), str(out))
    fc.main()
    assert len(read_citation_rows(str(out))) == 5


def test_no_previous_file_means_no_guard(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, _rows(["only"]))
    fc.main()
    assert len(read_citation_rows(str(out))) == 1


def test_zero_papers_is_always_refused(monkeypatch, tmp_path):
    _run_main(monkeypatch, tmp_path, [])
    with pytest.raises(RuntimeError) as excinfo:
        fc.main()
    assert "0 papers" in str(excinfo.value)


# ── parsing ──────────────────────────────────────────────────────────────────

ROW_HTML = '''<table><tbody>
<tr class="gsc_a_tr">
  <td class="gsc_a_t">
    <a class="gsc_a_at" href="/citations?view_op=view_citation&amp;hl=en&amp;user=U1&amp;citation_for_view=U1:ABC">A Paper Title</a>
    <div class="gs_gray">A Author and B Author</div>
    <div class="gs_gray">ACL 2024</div>
  </td>
  <td class="gsc_a_c"><a class="gsc_a_ac">42</a></td>
  <td class="gsc_a_y"><span>2024</span></td>
</tr></tbody></table>'''


def test_a_row_is_parsed_with_its_stable_id():
    (paper,) = fc._parse_page(ROW_HTML)
    assert paper["title"] == "A Paper Title"
    assert paper["scholar_id"] == "U1:ABC"
    assert paper["citations"] == "42"
    assert paper["venue"] == "ACL 2024"
    assert paper["authors"] == "A Author and B Author"


def test_a_captcha_page_raises_rather_than_returning_nothing():
    """Returning [] would look like an empty profile and truncate the file."""
    with pytest.raises(RuntimeError) as excinfo:
        fc._parse_page("<html><body>Please show you are not a robot: captcha</body></html>")
    assert "CAPTCHA" in str(excinfo.value)


def test_a_row_without_a_citation_count_parses_as_blank():
    html = ROW_HTML.replace('<a class="gsc_a_ac">42</a>', '')
    (paper,) = fc._parse_page(html)
    assert paper["citations"] == ""


def test_user_id_is_extracted_from_a_full_profile_url():
    assert fc._extract_user_id(
        "https://scholar.google.com/citations?user=ABC123&hl=en") == "ABC123"
    assert fc._extract_user_id("  ABC123  ") == "ABC123"
