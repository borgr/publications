"""scripts/refresh_venues.py: the script that rewrites the CV's venue prose.

Every sentence it generates is a factual claim printed on the author's CV --
"2nd of 20 in computational linguistics conferences by Google Scholar". Two
kinds of error matter, and neither announces itself:

  the wrong number   a name-matched venue that is not the venue, so another
                     conference's rank is attributed to this one
  a lost number      a refresh that drops a metric it did not fetch, which
                     silently demotes the sentence to a weaker claim

So the tests concentrate on the matching thresholds and on what survives a
partial refresh. Nothing here touches the network: _curl is replaced, and a
test that reaches it fails rather than fetching.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import refresh_venues as rv

from venues import Venues


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(rv, "_curl",
                        lambda url, browser=False: pytest.fail(f"unstubbed fetch: {url}"))
    monkeypatch.setattr(rv.time, "sleep", lambda _s: None)


# ── ordinals ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (7, "7th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (22, "22nd"), (23, "23rd"),
    (111, "111th"), (112, "112th"), (113, "113th"), (121, "121st"),
])
def test_ordinals(n, expected):
    """The hardcoded data this replaced said "1th" on a real CV."""
    assert rv.ordinal(n) == expected


# ── the fetch wrapper ────────────────────────────────────────────────────────

# Captured before the offline fixture replaces the module attribute, so these
# tests exercise the real wrapper while everything else stays unable to fetch.
_REAL_CURL = rv._curl
_BROWSER_UA_MARKER = "Chrome/"


class _Result:
    def __init__(self, returncode=0, stdout="body"):
        self.returncode, self.stdout = returncode, stdout


def run_curl(monkeypatch, result=None):
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return result or _Result()
    monkeypatch.setattr(rv.subprocess, "run", _run)
    return calls


def test_every_fetch_is_bounded_by_a_timeout(monkeypatch):
    """This script loops over every venue, sleeping between requests, and runs
    unattended. One socket that never closes hangs the whole refresh."""
    calls = run_curl(monkeypatch)
    _REAL_CURL("https://example.org/x")
    cmd, kwargs = calls[0]
    assert "--max-time" in cmd
    assert kwargs["timeout"] == 45


def test_a_failed_fetch_reads_as_empty_rather_than_as_content(monkeypatch):
    """curl prints its error page to stdout on some failures; returning it would
    be parsed as a ranking of zero venues instead of as an outage."""
    run_curl(monkeypatch, _Result(returncode=6, stdout="Could not resolve host"))
    assert _REAL_CURL("https://example.org/x") == ""


def test_scholar_gets_a_browser_user_agent(monkeypatch):
    """Scholar Metrics serves a CAPTCHA to anything that identifies as a script,
    and this page is the origin of every ranking on the CV."""
    calls = run_curl(monkeypatch)
    _REAL_CURL("https://scholar.google.com/citations", browser=True)
    joined = " ".join(calls[0][0])
    assert _BROWSER_UA_MARKER in joined
    assert "Accept-Language" in joined


def test_the_default_identity_says_what_the_script_is(monkeypatch):
    """Only Scholar needs a browser disguise; identifying honestly to OpenAlex is
    what its rate limiter asks for."""
    calls = run_curl(monkeypatch)
    _REAL_CURL("https://api.openalex.org/sources")
    assert "publications-venue-refresh/1.0" in calls[0][0]
    assert _BROWSER_UA_MARKER not in " ".join(calls[0][0])


# ── Scholar Metrics parsing ──────────────────────────────────────────────────

_SCHOLAR_HTML = """
<table><tbody>
  <tr><td>1.</td><td>Nature</td><td>488</td></tr>
  <tr><td>2.</td><td>Science</td><td>409</td></tr>
  <tr><td>3.</td><td>The Lancet</td><td>402</td></tr>
</tbody></table>
"""


def curl(monkeypatch, body, expect_browser=True):
    seen = {}

    def _stub(url, browser=False):
        seen["url"], seen["browser"] = url, browser
        return body
    monkeypatch.setattr(rv, "_curl", _stub)
    return seen


def test_a_category_page_yields_rank_name_and_h5(monkeypatch):
    curl(monkeypatch, _SCHOLAR_HTML)
    assert rv.fetch_scholar_category("eng") == [
        (1, "Nature", 488), (2, "Science", 409), (3, "The Lancet", 402)]


def test_the_rank_is_the_row_position_not_the_printed_number(monkeypatch):
    """Scholar prints "1." in a cell, but a page that ever renumbers or omits it
    must still produce ranks that count from one without gaps."""
    curl(monkeypatch, """<table><tbody>
      <tr><td></td><td>Nature</td><td>488</td></tr>
      <tr><td></td><td>Science</td><td>409</td></tr>
    </tbody></table>""")
    assert [r for r, _n, _h in rv.fetch_scholar_category("eng")] == [1, 2]


def test_scholar_is_fetched_with_a_browser_identity(monkeypatch):
    """The default user-agent gets a CAPTCHA rather than the rankings."""
    seen = curl(monkeypatch, _SCHOLAR_HTML)
    rv.fetch_scholar_category("eng")
    assert seen["browser"] is True
    assert seen["url"].endswith("vq=eng")


def test_a_captcha_is_reported_and_yields_nothing(monkeypatch, capsys):
    """Returning rows parsed out of a CAPTCHA page would write nonsense ranks."""
    curl(monkeypatch, "<html>Our systems have detected unusual traffic</html>")
    assert rv.fetch_scholar_category("eng") == []
    assert "CAPTCHA" in capsys.readouterr().err


def test_no_response_is_reported(monkeypatch, capsys):
    curl(monkeypatch, "")
    assert rv.fetch_scholar_category("eng") == []
    assert "no response" in capsys.readouterr().err


def test_a_page_that_parses_to_nothing_says_so(monkeypatch, capsys):
    """Silence here would look identical to a venue simply not being ranked, and
    the whole category would quietly stop being refreshed."""
    curl(monkeypatch, "<html><body><p>Rankings moved</p></body></html>")
    assert rv.fetch_scholar_category("eng") == []
    assert "may have changed" in capsys.readouterr().err


def test_rows_too_short_to_be_data_are_skipped(monkeypatch):
    """Scholar's markup includes layout rows with one or two cells; reading cell
    [2] on those would raise part-way through a loop over every category."""
    curl(monkeypatch, """<table><tbody>
      <tr><td colspan="3">Publications</td></tr>
      <tr><td>1.</td><td>Nature</td><td>488</td></tr>
    </tbody></table>""")
    assert rv.fetch_scholar_category("eng") == [(1, "Nature", 488)]


def test_rows_without_a_numeric_h5_are_skipped(monkeypatch):
    """Scholar's tables carry header and spacer rows shaped like data rows."""
    curl(monkeypatch, """<table><tbody>
      <tr><td>#</td><td>Publication</td><td>h5-index</td></tr>
      <tr><td>1.</td><td>Nature</td><td>488</td></tr>
    </tbody></table>""")
    assert rv.fetch_scholar_category("eng") == [(1, "Nature", 488)]


# ── matching a venue to its row ──────────────────────────────────────────────

_ROWS = [(1, "Nature", 488), (2, "Science", 409),
         (7, "Meeting of the Association for Computational Linguistics", 180)]


def test_an_exact_name_matches_regardless_of_case():
    assert rv.match_venue("science", _ROWS) == (2, "Science", 409)


def test_a_near_miss_still_matches():
    hit = rv.match_venue("Meeting of the Association for Computational Linguistic",
                         _ROWS)
    assert hit[0] == 7


def test_an_unrelated_name_matches_nothing():
    """Below the threshold the answer must be "not found", not the closest row:
    attaching Nature's rank to a workshop is worse than reporting nothing."""
    assert rv.match_venue("Workshop on Widgetry", _ROWS) is None


def test_a_substring_of_a_longer_name_is_not_enough():
    """"ACL" is a substring of the full ACL name but shares almost none of it, so
    the configured `name:` has to be the full string Scholar prints."""
    assert rv.match_venue("ACL", _ROWS) is None


def test_no_hint_matches_nothing():
    assert rv.match_venue("", _ROWS) is None


def test_an_empty_ranking_matches_nothing():
    assert rv.match_venue("Science", []) is None


# ── OpenAlex ─────────────────────────────────────────────────────────────────

_TACL = {"id": "https://openalex.org/S123",
         "display_name": "Transactions of the ACL",
         "summary_stats": {"2yr_mean_citedness": 8.234, "h_index": 71}}


def test_a_search_hit_is_reduced_to_the_four_fields_used(monkeypatch):
    curl(monkeypatch, json.dumps({"results": [_TACL]}))
    assert rv.fetch_openalex("Transactions of the ACL") == {
        "id": "S123", "display_name": "Transactions of the ACL",
        "2yr_mean_citedness": 8.23, "h_index": 71}


def test_citedness_is_rounded_so_the_cv_does_not_print_eight_decimals(monkeypatch):
    curl(monkeypatch, json.dumps({"results": [_TACL]}))
    assert rv.fetch_openalex("Transactions of the ACL")["2yr_mean_citedness"] == 8.23


def test_a_result_whose_name_does_not_resemble_the_query_is_refused(monkeypatch):
    """OpenAlex always returns its best guess, so a search for a venue it does not
    index comes back with a different journal -- and its impact metrics would be
    printed as this venue's."""
    curl(monkeypatch, """{"results": [{"id": "https://openalex.org/S9",
         "display_name": "Journal of Geophysical Research",
         "summary_stats": {"2yr_mean_citedness": 4.0, "h_index": 300}}]}""")
    assert rv.fetch_openalex("Transactions of the ACL") is None


def test_a_later_result_can_be_the_right_one(monkeypatch):
    """The first hit is not always the match; the name check decides, not the rank."""
    curl(monkeypatch, """{"results": [
        {"id": "https://openalex.org/S9", "display_name": "Unrelated Journal",
         "summary_stats": {}},
        {"id": "https://openalex.org/S123", "display_name": "Transactions of the ACL",
         "summary_stats": {"2yr_mean_citedness": 8.2, "h_index": 71}}]}""")
    assert rv.fetch_openalex("Transactions of the ACL")["id"] == "S123"


def test_an_empty_result_set_is_not_a_match(monkeypatch):
    curl(monkeypatch, '{"results": []}')
    assert rv.fetch_openalex("Transactions of the ACL") is None


def test_malformed_json_from_a_search_is_not_a_match(monkeypatch):
    """An outage page or a truncated response must not raise: this runs mid-way
    through a loop over every venue."""
    curl(monkeypatch, "<html>502 Bad Gateway</html>")
    assert rv.fetch_openalex("Transactions of the ACL") is None


def test_no_response_from_a_search_is_not_a_match(monkeypatch):
    curl(monkeypatch, "")
    assert rv.fetch_openalex("Transactions of the ACL") is None


def test_missing_summary_stats_read_as_zero_not_as_a_crash(monkeypatch):
    curl(monkeypatch, """{"results": [{"id": "https://openalex.org/S1",
         "display_name": "Transactions of the ACL"}]}""")
    found = rv.fetch_openalex("Transactions of the ACL")
    assert found["2yr_mean_citedness"] == 0 and found["h_index"] is None


def test_a_recorded_id_is_fetched_directly(monkeypatch):
    """Once an id is known it is used instead of searching, because a search can
    drift to a different venue between runs while an id cannot."""
    seen = curl(monkeypatch, json.dumps(_TACL))
    assert rv.fetch_openalex("Transactions of the ACL", "S123")["id"] == "S123"
    assert seen["url"].endswith("/sources/S123")


def test_a_dead_id_is_not_a_match(monkeypatch):
    curl(monkeypatch, '{"error": "Not found"}')
    assert rv.fetch_openalex("Transactions of the ACL", "S404") is None


def test_malformed_json_from_an_id_lookup_is_not_a_match(monkeypatch):
    curl(monkeypatch, "<html>502</html>")
    assert rv.fetch_openalex("Transactions of the ACL", "S123") is None


def test_a_contact_address_is_passed_to_openalex(monkeypatch):
    """OpenAlex asks for a mailto to put callers in its faster pool; without it a
    long refresh gets rate-limited part-way through."""
    monkeypatch.setattr(rv, "CONTACT", "a@example.org")
    seen = curl(monkeypatch, '{"results": []}')
    rv.fetch_openalex("Transactions of the ACL")
    assert "mailto=a%40example.org" in seen["url"]

    seen = curl(monkeypatch, '{"error": "x"}')
    rv.fetch_openalex("Transactions of the ACL", "S123")
    assert "mailto=a%40example.org" in seen["url"]


# ── the generated sentence ───────────────────────────────────────────────────

_CATEGORIES = {"eng": "computational linguistics"}


def test_a_ranked_conference_gets_the_ranking_sentence():
    entry = {"kind": "conference", "scholar_metrics": {"category": "eng"}}
    metrics = {"scholar_metrics": {"rank": 2, "total": 20}}
    assert (rv.describe(entry, metrics, _CATEGORIES) ==
            "2nd of 20 in computational linguistics conferences by Google Scholar")


def test_a_ranked_journal_is_not_called_a_conference():
    entry = {"kind": "journal", "scholar_metrics": {"category": "eng"}}
    metrics = {"scholar_metrics": {"rank": 1, "total": 20}}
    assert "computational linguistics venues" in rv.describe(entry, metrics, _CATEGORIES)


def test_an_unlabelled_category_falls_back_to_its_key():
    """A category added to a venue but not to the categories map still produces a
    readable sentence rather than a double space."""
    entry = {"kind": "conference", "scholar_metrics": {"category": "vision"}}
    metrics = {"scholar_metrics": {"rank": 3, "total": 20}}
    assert "in vision conferences" in rv.describe(entry, metrics, {})


def test_the_scholar_ranking_outranks_the_openalex_number():
    """A rank among named peers says more on a CV than a citedness figure, so when
    both are known the ranking wins."""
    entry = {"kind": "conference", "scholar_metrics": {"category": "eng"}}
    metrics = {"scholar_metrics": {"rank": 2, "total": 20},
               "openalex": {"2yr_mean_citedness": 8.2}}
    assert "Google Scholar" in rv.describe(entry, metrics, _CATEGORIES)


def test_openalex_is_used_when_there_is_no_ranking():
    entry = {"kind": "journal"}
    metrics = {"openalex": {"2yr_mean_citedness": 8.2}}
    assert (rv.describe(entry, metrics, _CATEGORIES) ==
            "Journal with a 2-year mean citedness of 8.2 (OpenAlex)")


def test_a_non_journal_with_only_openalex_is_called_a_venue():
    entry = {"kind": "conference"}
    metrics = {"openalex": {"2yr_mean_citedness": 3.1}}
    assert rv.describe(entry, metrics, _CATEGORIES).startswith("Venue with")


@pytest.mark.parametrize("metrics", [
    {},
    {"scholar_metrics": {"rank": 2}},
    {"scholar_metrics": {"total": 20}},
    {"openalex": {"2yr_mean_citedness": 0}},
])
def test_incomplete_metrics_produce_no_sentence(metrics):
    """An empty string leaves the existing description alone; a half-built
    sentence ("2nd of None in ...") would be printed on the CV."""
    assert rv.describe({"kind": "journal"}, metrics, _CATEGORIES) == ""


# ── main ─────────────────────────────────────────────────────────────────────

def venue_file(**venues):
    return {"scholar_metrics": {"categories": dict(_CATEGORIES)},
            "venues": venues}


@pytest.fixture
def run(monkeypatch):
    """Run main() against an in-memory venues.yaml, and hand back the result.

    save() is recorded rather than performed: the real file is tracked in git, and
    a test that wrote it would leave the working tree dirty and make CI's
    staleness check fail for an unrelated reason.
    """
    def _run(data, argv=(), scholar=None, openalex=None):
        loaded = Venues(data)
        saves = []
        monkeypatch.setattr(rv.Venues, "load",
                            classmethod(lambda cls, path=None: loaded))
        monkeypatch.setattr(rv.Venues, "save",
                            lambda self, path=None: saves.append(True))
        monkeypatch.setattr(rv, "fetch_scholar_category",
                            scholar or (lambda category: []))
        monkeypatch.setattr(rv, "fetch_openalex",
                            openalex or (lambda search, source_id=None: None))
        code = rv.main(list(argv))
        return loaded, saves, code
    return _run


def test_an_empty_venues_file_is_an_error(run, capsys):
    _v, saves, code = run({})
    assert code == 1 and saves == []
    assert "No venues found" in capsys.readouterr().err


def test_a_ranked_venue_gets_its_metrics_and_description(run):
    data = venue_file(acl={"kind": "conference",
                           "scholar_metrics": {"category": "eng", "name": "Nature"},
                           "description": "stale"})
    loaded, saves, code = run(data, scholar=lambda category: _ROWS)
    entry = loaded.venues["acl"]
    assert code == 0 and saves == [True]
    assert entry["metrics"]["scholar_metrics"]["rank"] == 1
    assert entry["metrics"]["scholar_metrics"]["total"] == 3
    assert entry["description"].startswith("1st of 3 in computational linguistics")
    assert entry["metrics"]["refreshed"]


def test_the_venue_key_is_used_when_no_name_is_configured(run):
    data = venue_file(nature={"kind": "journal", "scholar_metrics": {"category": "eng"}})
    loaded, _s, _c = run(data, scholar=lambda category: _ROWS)
    assert loaded.venues["nature"]["metrics"]["scholar_metrics"]["rank"] == 1


def test_a_manual_venue_keeps_its_prose_but_gets_fresh_metrics(run):
    """Journal prose is hand-written because the metric a journal is judged by is
    proprietary and cannot be looked up -- but the numbers still refresh."""
    data = venue_file(tacl={"kind": "journal", "manual": True,
                            "openalex": {"search": "Transactions of the ACL"},
                            "description": "The flagship journal of the field"})
    loaded, _s, _c = run(data, openalex=lambda search, source_id=None: dict(
        _TACL, id="S123", **{"2yr_mean_citedness": 8.2, "h_index": 71}))
    entry = loaded.venues["tacl"]
    assert entry["description"] == "The flagship journal of the field"
    assert entry["metrics"]["openalex"]["2yr_mean_citedness"] == 8.2


def test_refreshing_one_source_does_not_delete_the_other(run):
    """--source openalex used to rebuild the metrics block from scratch, dropping
    the Scholar ranking it had not fetched. describe() then rewrote the sentence
    from "2nd of 20 ... by Google Scholar" down to a citedness figure -- a silent
    demotion of every ranked venue, written straight to venues.yaml."""
    ranking = "2nd of 20 in computational linguistics conferences by Google Scholar"
    data = venue_file(acl={
        "kind": "conference",
        "scholar_metrics": {"category": "eng", "name": "Nature"},
        "openalex": {"search": "Transactions of the ACL"},
        "metrics": {"scholar_metrics": {"rank": 2, "total": 20, "h5_index": 100}},
        "description": ranking})
    loaded, _s, _c = run(data, argv=["--source", "openalex"],
                         openalex=lambda search, source_id=None: {
                             "id": "S123", "display_name": "Transactions of the ACL",
                             "2yr_mean_citedness": 8.2, "h_index": 71})
    entry = loaded.venues["acl"]
    assert entry["metrics"]["scholar_metrics"]["rank"] == 2
    assert entry["metrics"]["openalex"]["2yr_mean_citedness"] == 8.2
    assert entry["description"] == ranking


def test_source_openalex_makes_no_scholar_request(run):
    data = venue_file(acl={"kind": "conference",
                           "scholar_metrics": {"category": "eng", "name": "Nature"}})
    run(data, argv=["--source", "openalex"],
        scholar=lambda category: pytest.fail("fetched Scholar under --source openalex"))


def test_source_scholar_makes_no_openalex_request(run):
    data = venue_file(tacl={"kind": "journal",
                            "openalex": {"search": "Transactions of the ACL"}})
    run(data, argv=["--source", "scholar"],
        openalex=lambda search, source_id=None: pytest.fail(
            "fetched OpenAlex under --source scholar"))


def test_a_resolved_openalex_id_is_recorded_for_next_time(run):
    """Pinning the id is what stops a later run's search drifting to a different
    venue and rewriting the sentence with its numbers."""
    data = venue_file(tacl={"kind": "journal",
                            "openalex": {"search": "Transactions of the ACL"}})
    loaded, _s, _c = run(data, openalex=lambda search, source_id=None: {
        "id": "S123", "display_name": "Transactions of the ACL",
        "2yr_mean_citedness": 8.2, "h_index": 71})
    assert loaded.venues["tacl"]["openalex"]["id"] == "S123"


def test_an_existing_id_is_not_overwritten(run):
    data = venue_file(tacl={"kind": "journal",
                            "openalex": {"search": "Transactions of the ACL",
                                         "id": "S-PINNED"}})
    loaded, _s, _c = run(data, openalex=lambda search, source_id=None: {
        "id": "S-DRIFTED", "display_name": "Transactions of the ACL",
        "2yr_mean_citedness": 8.2, "h_index": 71})
    assert loaded.venues["tacl"]["openalex"]["id"] == "S-PINNED"


def test_a_venue_no_source_could_resolve_is_reported(run, capsys):
    data = venue_file(
        obscure={"kind": "conference",
                 "scholar_metrics": {"category": "eng", "name": "Workshop on Widgetry"},
                 "openalex": {"search": "Workshop on Widgetry"}})
    loaded, _s, code = run(data, scholar=lambda category: _ROWS)
    out = capsys.readouterr().out
    assert code == 0
    assert "not found in its Scholar Metrics category" in out
    assert "no confident OpenAlex match" in out
    assert "metrics" not in loaded.venues["obscure"], (
        "a venue nothing resolved must not get a refreshed-at stamp")


def test_a_venue_with_no_sources_configured_is_left_alone(run):
    data = venue_file(local={"kind": "conference", "description": "hand-written"})
    loaded, _s, _c = run(data)
    assert loaded.venues["local"] == {"kind": "conference", "description": "hand-written"}


def test_an_unchanged_description_is_not_reported_as_a_change(run, capsys):
    """Otherwise every run prints every venue and the real changes are unreadable."""
    data = venue_file(nature={"kind": "journal",
                              "scholar_metrics": {"category": "eng"},
                              "description": "1st of 3 in computational linguistics "
                                             "venues by Google Scholar"})
    run(data, scholar=lambda category: _ROWS)
    assert "No description changes." in capsys.readouterr().out


def test_a_changed_description_prints_both_versions(run, capsys):
    """This is the review surface: the diff is what a person checks before the CV
    goes out claiming a different rank."""
    data = venue_file(nature={"kind": "journal", "scholar_metrics": {"category": "eng"},
                              "description": "was something else"})
    run(data, scholar=lambda category: _ROWS)
    out = capsys.readouterr().out
    assert "was: was something else" in out
    assert "now: 1st of 3" in out


def test_a_dry_run_writes_nothing_but_still_reports(run, capsys):
    data = venue_file(nature={"kind": "journal", "scholar_metrics": {"category": "eng"},
                              "description": "was something else"})
    loaded, saves, code = run(data, argv=["--dry-run"], scholar=lambda category: _ROWS)
    assert code == 0 and saves == []
    assert "dry-run" in capsys.readouterr().out
    assert loaded.venues["nature"]["metrics"], "the report needs the fresh numbers"


def test_a_null_venue_entry_does_not_crash_the_run(run):
    """`acl:` with nothing under it is valid YAML and reads as None."""
    _loaded, _s, code = run(venue_file(acl=None))
    assert code == 0


# ── the real venues.yaml ─────────────────────────────────────────────────────

def test_every_configured_scholar_category_is_defined():
    """A typo in a `category:` makes that venue silently unrefreshable forever:
    its category fetches nothing, so it is never even reported as unresolved."""
    venues = Venues.load()
    known = set(venues.categories)
    for key, entry in venues.venues.items():
        category = ((entry or {}).get("scholar_metrics") or {}).get("category")
        if category:
            assert category in known, f"{key} names undefined category {category!r}"
