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


def test_a_row_with_no_title_is_skipped():
    """Scholar's table ends with a "show more" row shaped like a paper row.
    Admitting it adds a titleless paper that no later step can match or resolve."""
    html = ROW_HTML.replace(">A Paper Title<", "><")
    assert fc._parse_page(html) == []


def test_a_row_missing_its_gray_divs_parses_with_blanks():
    """A profile entry with no venue is normal -- an unpublished manuscript -- and
    must not take the rest of the page down with it."""
    html = ROW_HTML.replace('<div class="gs_gray">ACL 2024</div>', '')
    (paper,) = fc._parse_page(html)
    assert paper["venue"] == "" and paper["authors"] == "A Author and B Author"


def test_a_row_missing_its_year_parses_as_blank():
    html = ROW_HTML.replace('<td class="gsc_a_y"><span>2024</span></td>', '')
    (paper,) = fc._parse_page(html)
    assert paper["year"] == ""


def test_an_href_without_the_view_parameter_degrades_to_no_id():
    """Losing the ID costs exactness in the citation join, which is recoverable;
    raising here would cost the whole run, which is not."""
    html = ROW_HTML.replace("&amp;citation_for_view=U1:ABC", "")
    (paper,) = fc._parse_page(html)
    assert paper["scholar_id"] == ""


def test_an_absent_anchor_yields_no_id():
    assert fc._extract_scholar_id(None) == ""


def test_unusual_traffic_is_treated_as_a_captcha():
    """Scholar's two block pages differ in wording, and only one says "captcha"."""
    with pytest.raises(RuntimeError):
        fc._parse_page("<html><body>We have detected unusual traffic</body></html>")


def test_user_id_is_extracted_from_a_full_profile_url():
    assert fc._extract_user_id(
        "https://scholar.google.com/citations?user=ABC123&hl=en") == "ABC123"
    assert fc._extract_user_id("  ABC123  ") == "ABC123"


def test_a_profile_url_with_no_user_parameter_is_rejected():
    """Better than scraping `user=` empty, which returns somebody else's page."""
    with pytest.raises(ValueError) as excinfo:
        fc._extract_user_id("https://scholar.google.com/citations?hl=en")
    assert "Could not extract user=" in str(excinfo.value)


# ── the HTTP layer ───────────────────────────────────────────────────────────

class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_curl(monkeypatch, result):
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return result
    monkeypatch.setattr(fc.subprocess, "run", _run)
    return calls


def test_the_status_code_is_separated_from_the_body(monkeypatch):
    run_curl(monkeypatch, _Result(stdout="<html>hi</html>\n__STATUS__200"))
    assert fc._curl_get("https://example.org") == (200, "<html>hi</html>")


def test_a_body_containing_the_marker_is_not_truncated(monkeypatch):
    """The marker is appended by curl, so only the *last* one is the status; a page
    that mentions it must still be parsed in full rather than cut at the mention."""
    run_curl(monkeypatch, _Result(stdout="a\n__STATUS__b\n__STATUS__404"))
    status, body = fc._curl_get("https://example.org")
    assert (status, body) == (404, "a\n__STATUS__b")


def test_a_curl_that_could_not_connect_raises(monkeypatch):
    """Distinguished from an HTTP error on purpose: a 429 is retried, a broken
    network is not, and treating one as the other burns the retry budget."""
    run_curl(monkeypatch, _Result(returncode=6, stderr="Could not resolve host"))
    with pytest.raises(RuntimeError) as excinfo:
        fc._curl_get("https://example.org")
    assert "Could not resolve host" in str(excinfo.value)


def test_output_with_no_status_marker_raises(monkeypatch):
    run_curl(monkeypatch, _Result(stdout="just a body, no marker"))
    with pytest.raises(RuntimeError) as excinfo:
        fc._curl_get("https://example.org")
    assert "Unexpected curl output" in str(excinfo.value)


def test_every_request_is_bounded_and_keeps_its_cookies(monkeypatch):
    """The cookie jar is what makes the second page load: without it Scholar treats
    each request as a new session and starts serving CAPTCHAs mid-scrape."""
    calls = run_curl(monkeypatch, _Result(stdout="body\n__STATUS__200"))
    fc._curl_get("https://example.org")
    cmd, kwargs = calls[0]
    assert "--max-time" in cmd and "--cookie-jar" in cmd
    assert kwargs["timeout"] == 40


# ── retrying a rate-limited page ─────────────────────────────────────────────

@pytest.fixture
def responses(monkeypatch):
    """Serve a queued list of (status, body) to _fetch_page, recording sleeps."""
    slept = []
    monkeypatch.setattr(fc.time, "sleep", lambda s: slept.append(s))

    def _serve(queue):
        remaining = list(queue)
        monkeypatch.setattr(fc, "_curl_get", lambda url: remaining.pop(0))
        return slept
    return _serve


def test_a_page_is_returned_on_the_first_success(responses):
    slept = responses([(200, "<html>page</html>")])
    assert fc._fetch_page("USER", 0) == "<html>page</html>"
    assert slept == [], "a successful fetch must not pause"


def test_a_rate_limited_page_is_retried_after_waiting(responses):
    """429 is what Scholar returns to a long scrape, so giving up on the first one
    would cap the profile at whatever had loaded."""
    slept = responses([(429, ""), (200, "<html>page</html>")])
    assert fc._fetch_page("USER", 20) == "<html>page</html>"
    assert slept == [30]


def test_the_wait_grows_with_each_refusal(responses):
    """A fixed wait against a rate limiter mostly just keeps hitting it."""
    slept = responses([(429, ""), (429, ""), (429, ""), (200, "ok")])
    fc._fetch_page("USER", 0)
    assert slept == [30, 60, 120]


def test_a_persistent_rate_limit_raises_rather_than_returning_nothing(responses):
    """Returning "" would parse as an empty page, and an empty page is how the
    scraper decides the profile has ended -- silently truncating it."""
    responses([(429, "")] * fc._MAX_RETRIES)
    with pytest.raises(RuntimeError) as excinfo:
        fc._fetch_page("USER", 0)
    assert "persistent 429" in str(excinfo.value)


def test_any_other_http_error_raises_immediately(responses):
    slept = responses([(503, "")])
    with pytest.raises(RuntimeError) as excinfo:
        fc._fetch_page("USER", 0)
    assert "HTTP 503" in str(excinfo.value)
    assert slept == [], "a 503 is not rate limiting; retrying it just wastes the budget"


# ── profile stats ────────────────────────────────────────────────────────────

STATS_HTML = '''<table id="gsc_rsb_st"><tbody>
  <tr><td class="gsc_rsb_sc1">Citations</td>
      <td class="gsc_rsb_std">1,234</td><td class="gsc_rsb_std">567</td></tr>
  <tr><td class="gsc_rsb_sc1">h-index</td>
      <td class="gsc_rsb_std">21</td><td class="gsc_rsb_std">18</td></tr>
  <tr><td class="gsc_rsb_sc1">i10-index</td>
      <td class="gsc_rsb_std">30</td><td class="gsc_rsb_std">25</td></tr>
</tbody></table>'''


def test_the_three_profile_numbers_are_parsed():
    assert fc._parse_profile_stats(STATS_HTML) == {
        "citations": 1234, "h_index": 21, "i10_index": 30}


def test_the_all_time_column_is_taken_not_the_last_five_years():
    """Scholar prints both, and a CV that quoted the five-year column would
    understate every number without looking wrong."""
    assert fc._parse_profile_stats(STATS_HTML)["citations"] == 1234


def test_thousands_separators_are_stripped():
    """Left in, int() raises and the whole run dies at the first request -- and it
    only starts happening once the author passes a thousand citations."""
    assert fc._parse_profile_stats(STATS_HTML)["citations"] == 1234


def test_a_page_without_the_stats_table_yields_none():
    """None is the signal to warn and carry on. The papers are the point; the
    summary numbers are not worth failing a run over."""
    assert fc._parse_profile_stats("<html><body>no table</body></html>") is None


def test_an_unparseable_value_is_skipped_not_fatal():
    html = STATS_HTML.replace(">1,234<", ">—<")
    stats = fc._parse_profile_stats(html)
    assert "citations" not in stats
    assert stats["h_index"] == 21


def test_a_table_of_nothing_parseable_yields_none():
    html = STATS_HTML.replace(">1,234<", ">—<").replace(">21<", ">—<") \
                     .replace(">30<", ">—<")
    assert fc._parse_profile_stats(html) is None


def test_an_unrecognised_row_label_is_ignored():
    """Scholar adds rows over time; an unknown one must not become a stats key."""
    html = STATS_HTML.replace(
        "<tr><td class=\"gsc_rsb_sc1\">Citations</td>",
        "<tr><td class=\"gsc_rsb_sc1\">Reads</td>"
        "<td class=\"gsc_rsb_std\">9</td></tr>"
        "<tr><td class=\"gsc_rsb_sc1\">Citations</td>")
    assert set(fc._parse_profile_stats(html)) == {"citations", "h_index", "i10_index"}


def test_a_row_with_no_values_is_ignored():
    html = STATS_HTML.replace(
        '<td class="gsc_rsb_std">1,234</td><td class="gsc_rsb_std">567</td>', '')
    assert "citations" not in fc._parse_profile_stats(html)


# ── writing the stats file ───────────────────────────────────────────────────

def test_stats_are_written_as_json(tmp_path):
    path = str(tmp_path / "profile_stats.json")
    fc.write_stats({"citations": 5, "h_index": 2}, path)
    import json
    assert json.load(open(path)) == {"citations": 5, "h_index": 2}
    assert not os.path.exists(path + ".tmp")


def test_a_failed_stats_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    path = str(tmp_path / "profile_stats.json")
    fc.write_stats({"citations": 5}, path)

    def explode(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(fc.json, "dump", explode)
    with pytest.raises(OSError):
        fc.write_stats({"citations": 9}, path)
    import json
    assert json.load(open(path)) == {"citations": 5}
    assert not os.path.exists(path + ".tmp"), "a stale temp file was left behind"


# ── warnings that a selector has stopped working ─────────────────────────────

def test_missing_citation_counts_everywhere_is_a_warning(scholar, monkeypatch, capsys):
    """Each paper reading zero is indistinguishable from a genuinely uncited
    profile, so the only way to notice a renamed selector is to say so."""
    blank = [{**p, "citations": ""} for p in _rows(["a", "b"])]
    monkeypatch.setattr(fc, "_parse_page", lambda html: blank if html == "__PAGE_0__" else [])
    fc.scrape_profile("USER", delay=0)
    assert "no citation counts found" in capsys.readouterr().err


def test_missing_stable_ids_everywhere_is_a_warning(scholar, monkeypatch, capsys):
    """Without them the citation join silently drops from an exact lookup to title
    similarity, which is where the wrong-paper errors come from."""
    no_ids = [{**p, "scholar_id": ""} for p in _rows(["a", "b"])]
    monkeypatch.setattr(fc, "_parse_page", lambda html: no_ids if html == "__PAGE_0__" else [])
    fc.scrape_profile("USER", delay=0)
    assert "no citation_for_view IDs found" in capsys.readouterr().err


def test_unparseable_profile_stats_are_reported_but_not_fatal(scholar, monkeypatch, capsys):
    monkeypatch.setattr(fc, "_parse_profile_stats", lambda html: None)
    papers, stats = fc.scrape_profile("USER", delay=0)
    assert stats is None and len(papers) == 50
    assert "could not parse profile stats" in capsys.readouterr().out


def test_a_missing_curl_is_refused_before_anything_is_fetched(monkeypatch):
    monkeypatch.setattr(fc.shutil, "which", lambda _name: None)
    monkeypatch.setattr(fc, "_curl_get",
                        lambda url: pytest.fail("fetched without curl present"))
    with pytest.raises(RuntimeError) as excinfo:
        fc.scrape_profile("USER", delay=0)
    assert "curl is required" in str(excinfo.value)


# ── the stats side-file ──────────────────────────────────────────────────────

def test_stats_land_next_to_the_csv(monkeypatch, tmp_path):
    """Not next to fetch_citations.py: a fork running with -o elsewhere would
    otherwise write its stats into the checkout it forked from."""
    _run_main(monkeypatch, tmp_path, _rows(["one"]))
    fc.main()
    assert (tmp_path / "profile_stats.json").exists()


def test_no_stats_file_is_written_when_they_could_not_be_parsed(monkeypatch, tmp_path):
    out = tmp_path / "citations.csv"
    monkeypatch.setattr(fc, "scrape_profile", lambda user, **kw: (_rows(["one"]), None))
    monkeypatch.setattr(sys, "argv",
                        ["fetch_citations.py", "USER", "-o", str(out)])
    fc.main()
    assert not (tmp_path / "profile_stats.json").exists()
    assert len(read_citation_rows(str(out))) == 1, "the papers still get written"
