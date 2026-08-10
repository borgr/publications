"""The five external sources, and what they are allowed to put in the CV.

test_resolve.py covers which results may replace an existing entry. This file
covers the layer beneath: turning each source's response into BibTeX. That is
where a wrong answer is most expensive, because none of these sources returns
"no" -- OpenAlex answered a query about MuLER with a paper on European climate
modelling, and DBLP returns five results whether or not any is the right paper.
An unrejected result is silently published under the author's name.

Everything here is offline: _curl_get and _http_get_json are the only two doors
out, and both are stubbed. A test that reaches the network is a test that fails
on a plane and in a fork's CI.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolve_arxiv as ra
from bib_utils import extract_field, is_wellformed_entry

# Captured before the offline fixture replaces it with a tripwire, so the tests
# of _curl_get itself can call the real thing.
_REAL_CURL_GET = ra._curl_get


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network, no sleeping, and no S2 cooldown inherited from another test.

    _s2_state and _net_state are module-global: a test that trips the cooldown
    would otherwise make every later S2 test see a paused source and pass for the
    wrong reason, and a leftover unanswered count would make a later test think
    its own lookup went missing.
    """
    monkeypatch.setattr(ra, "_curl_get",
                        lambda url, **kw: pytest.fail(f"unstubbed network call: {url}"))
    monkeypatch.setattr(ra.time, "sleep", lambda _s: None)
    monkeypatch.setitem(ra._s2_state, "blocked_until", 0.0)
    monkeypatch.setitem(ra._net_state, "unanswered", 0)


def curl(monkeypatch, responses):
    """Stub _curl_get with a url-substring -> body mapping.

    Honours the real contract, so callers are tested against it: a body the
    caller's own `accept` predicate rejects comes back as None ("no answer"),
    not as "" ("answered, has nothing").
    """
    def _get(url, accept=None, tries=1):
        for fragment, body in responses.items():
            if fragment in url:
                if body and accept is not None and not accept(body):
                    ra._note_unanswered(fragment)
                    return None
                return body
        return ""
    monkeypatch.setattr(ra, "_curl_get", _get)


# ── the S2 key and its cooldown ──────────────────────────────────────────────

@pytest.fixture
def no_key(tmp_path, monkeypatch):
    """Every source of the key, pointed somewhere disposable.

    KEY_FILE included, and not only for isolation: left alone these tests read the
    real key on the author's machine, and a failing assertion prints what it
    compared. A test suite is not a place a credential should be able to surface.
    """
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.setattr(ra, "KEY_FILE", str(tmp_path / "s2_api_key"))
    monkeypatch.setattr(ra, "FILE_SOURCE", str(tmp_path / "s2_api_key"))
    import config
    monkeypatch.setattr(config, "S2_API_KEY", "", raising=False)
    return tmp_path


def test_the_api_key_comes_from_the_environment(no_key, monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "  env-key  ")
    assert ra.s2_api_key() == "env-key"


def test_no_key_configured_is_not_an_error(no_key):
    """S2 works unauthenticated, just with more 429s, so a missing key must
    degrade rather than raise."""
    assert ra.s2_api_key() == ""
    assert ra.s2_api_key_source() == ("", "")


# The key file is the only source the weekly run can see: launchd gives a job PATH
# and HOME, not the shell's exports, and config.py cannot hold a key because it is
# tracked in a public repository. So these are the tests that decide whether the
# unattended run is authenticated at all.

def test_the_key_file_is_read_and_trimmed(no_key):
    """Written with a text editor or `echo`, so it ends in a newline. Sent as an
    HTTP header, where a trailing newline is not a value, it is an invalid header."""
    (no_key / "s2_api_key").write_text("file-key\n")
    assert ra.s2_api_key() == "file-key"


def test_the_environment_wins_over_the_key_file(no_key, monkeypatch):
    """So CI can inject a key for one run without writing a credential to disk."""
    (no_key / "s2_api_key").write_text("file-key\n")
    monkeypatch.setenv("S2_API_KEY", "env-key")
    assert ra.s2_api_key() == "env-key"


def test_the_key_file_wins_over_config(no_key, monkeypatch):
    import config
    monkeypatch.setattr(config, "S2_API_KEY", "config-key", raising=False)
    (no_key / "s2_api_key").write_text("file-key\n")
    assert ra.s2_api_key() == "file-key"


def test_config_is_still_honoured_for_a_fork_that_keeps_it_private(no_key, monkeypatch):
    import config
    monkeypatch.setattr(config, "S2_API_KEY", "config-key", raising=False)
    assert ra.s2_api_key_source() == ("config-key", ra.CONFIG_SOURCE)


def test_an_empty_key_file_falls_through_instead_of_masking_config(no_key, monkeypatch):
    """A file left behind by a rotation, or truncated by a failed write. Treating
    it as a key would authenticate with the empty string and read as "throttled for
    no reason" -- while config.py sat there holding a working one."""
    import config
    monkeypatch.setattr(config, "S2_API_KEY", "config-key", raising=False)
    (no_key / "s2_api_key").write_text("\n")
    assert ra.s2_api_key() == "config-key"


def test_an_unreadable_key_file_is_not_an_error(no_key):
    """A directory where the file should be, or one saved with no read permission.
    The pipeline works unauthenticated, so this may not take the run down."""
    (no_key / "s2_api_key").mkdir()
    assert ra.s2_api_key() == ""


def test_each_source_names_itself(no_key, monkeypatch):
    """The source is printed, so a key that is being shadowed can be found. Naming
    the wrong one would send someone to edit a file that is not in play."""
    monkeypatch.setenv("S2_API_KEY", "k")
    assert ra.s2_api_key_source()[1] == ra.ENV_SOURCE
    monkeypatch.delenv("S2_API_KEY")
    (no_key / "s2_api_key").write_text("k")
    assert ra.s2_api_key_source()[1] == str(no_key / "s2_api_key")


def test_the_key_file_lives_outside_the_repository():
    """Inside it, one `git add -A` publishes the key -- the mistake the whole
    arrangement exists to prevent. Deliberately not using the `no_key` fixture:
    the value under test is the module's own default, not a stub of it."""
    repo = os.path.dirname(os.path.abspath(ra.__file__))
    assert not os.path.abspath(ra.KEY_FILE).startswith(repo + os.sep)
    assert os.path.isabs(ra.KEY_FILE), "a relative path resolves against the cwd"


def test_s2_is_available_when_not_in_cooldown():
    assert ra.s2_available() is True


def test_s2_is_unavailable_during_the_cooldown(monkeypatch):
    monkeypatch.setattr(ra.time, "time", lambda: 100.0)
    monkeypatch.setitem(ra._s2_state, "blocked_until", 160.0)
    assert ra.s2_available() is False


def test_the_cooldown_expires_and_says_so(monkeypatch, capsys):
    """It has to expire on its own. Disabling S2 for a whole run was the worse
    bug: the ACL Anthology and OpenReview are both reached through S2, so losing
    it lost all three sources."""
    monkeypatch.setattr(ra.time, "time", lambda: 200.0)
    monkeypatch.setitem(ra._s2_state, "blocked_until", 160.0)
    assert ra.s2_available() is True
    assert "cooldown over" in capsys.readouterr().out
    assert ra._s2_state["blocked_until"] == 0.0


# ── _http_get_json ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_json_is_returned_on_success(monkeypatch):
    monkeypatch.setattr(ra, "urlopen", lambda req, timeout=0: _Resp({"a": 1}))
    assert ra._http_get_json("https://x/") == {"a": 1}


def test_the_api_key_is_sent_as_a_header(monkeypatch):
    seen = {}
    monkeypatch.setenv("S2_API_KEY", "secret-key")

    def _open(req, timeout=0):
        seen.update(req.headers)
        return _Resp({})
    monkeypatch.setattr(ra, "urlopen", _open)
    ra._http_get_json("https://x/")
    # urllib title-cases header names.
    assert seen.get("X-api-key") == "secret-key"


def test_no_request_is_made_while_s2_is_paused(monkeypatch):
    monkeypatch.setattr(ra.time, "time", lambda: 0.0)
    monkeypatch.setitem(ra._s2_state, "blocked_until", 99.0)
    monkeypatch.setattr(ra, "urlopen",
                        lambda *a, **k: pytest.fail("called S2 during its cooldown"))
    assert ra._http_get_json("https://x/") is None


def test_a_429_is_retried_once_before_giving_up(monkeypatch):
    calls = []

    def _open(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("HTTP Error 429: Too Many Requests")
        return _Resp({"ok": True})
    monkeypatch.setattr(ra, "urlopen", _open)
    assert ra._http_get_json("https://x/") == {"ok": True}
    assert len(calls) == 2


def test_a_second_429_pauses_s2_rather_than_disabling_it(monkeypatch, capsys):
    monkeypatch.setattr(ra.time, "time", lambda: 1000.0)

    def _open(req, timeout=0):
        raise OSError("HTTP Error 429")
    monkeypatch.setattr(ra, "urlopen", _open)
    assert ra._http_get_json("https://x/") is None
    assert ra._s2_state["blocked_until"] == 1000.0 + ra._S2_COOLDOWN_SECONDS
    assert "pausing it" in capsys.readouterr().out


def test_a_code_attribute_counts_as_a_429_too(monkeypatch):
    """urllib raises HTTPError, whose status is on `.code` and not necessarily in
    its str()."""
    class Boom(OSError):
        code = 429

    monkeypatch.setattr(ra.time, "time", lambda: 0.0)
    monkeypatch.setattr(ra, "urlopen", lambda *a, **k: (_ for _ in ()).throw(Boom()))
    assert ra._http_get_json("https://x/") is None
    assert ra._s2_state["blocked_until"] > 0


def test_a_non_429_error_returns_none_without_pausing_s2(monkeypatch, capsys):
    """A 404 for one paper says nothing about the next one."""
    monkeypatch.setattr(ra, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("404")))
    assert ra._http_get_json("https://x/", retries=1) is None
    assert ra._s2_state["blocked_until"] == 0.0
    assert "HTTP error" in capsys.readouterr().err


def test_a_transient_error_is_retried(monkeypatch):
    calls = []

    def _open(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("connection reset")
        return _Resp({"ok": 1})
    monkeypatch.setattr(ra, "urlopen", _open)
    assert ra._http_get_json("https://x/") == {"ok": 1}


# ── attempt counts ───────────────────────────────────────────────────────────

def test_attempts_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "ATTEMPTS_PATH", str(tmp_path / "attempts.json"))
    ra.save_attempts({"a": 3, "b": 1})
    assert ra.load_attempts() == {"a": 3, "b": 1}


def test_a_missing_attempts_file_is_an_empty_history(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "ATTEMPTS_PATH", str(tmp_path / "nope.json"))
    assert ra.load_attempts() == {}


def test_a_corrupt_attempts_file_does_not_stop_the_run(tmp_path, monkeypatch):
    """It is a cache of retry counts. Losing it costs some wasted lookups; raising
    here would cost the whole run."""
    path = tmp_path / "attempts.json"
    path.write_text("{not json")
    monkeypatch.setattr(ra, "ATTEMPTS_PATH", str(path))
    assert ra.load_attempts() == {}


def test_saving_attempts_leaves_no_temporary_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "ATTEMPTS_PATH", str(tmp_path / "attempts.json"))
    ra.save_attempts({"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["attempts.json"]


# ── a failed request is not an answer ────────────────────────────────────────
#
# _curl_get is the one door out for four of the five sources, so this is where
# "nobody replied" either stays distinguishable from "nobody has it" or stops
# being distinguishable for the whole pipeline.

class _Result:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout = returncode, stdout


def _curl_returning(monkeypatch, *results):
    """Stub subprocess.run so the real _curl_get runs. Returns the call log.

    Puts the real _curl_get back, since the offline fixture replaces it with a
    tripwire -- these are the tests of that function itself.
    """
    calls = []

    def _run(argv, **_kw):
        calls.append(argv[-1])
        result = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(ra, "_curl_get", _REAL_CURL_GET)
    monkeypatch.setattr(ra.subprocess, "run", _run)
    return calls


def test_a_successful_empty_body_is_an_answer(monkeypatch):
    """Retrying it would cost three requests for every paper no source indexes."""
    calls = _curl_returning(monkeypatch, _Result(0, ""))
    assert ra._curl_get("https://dblp.org/x") == ""
    assert len(calls) == 1
    assert ra.unanswered_lookups() == 0


def test_a_transport_failure_is_retried_and_then_reported_as_no_answer(monkeypatch):
    calls = _curl_returning(monkeypatch, _Result(7, ""))
    assert ra._curl_get("https://dblp.org/x") is None
    assert len(calls) == ra._CURL_TRIES
    assert ra.unanswered_lookups() == 1


def test_a_retry_that_succeeds_returns_the_body(monkeypatch):
    """The failure mode being fixed was transient: the same query returned
    nothing, then two results ten seconds later."""
    _curl_returning(monkeypatch, _Result(7, ""), _Result(0, "@article{a,}"))
    assert ra._curl_get("https://dblp.org/x") == "@article{a,}"
    assert ra.unanswered_lookups() == 0


def test_a_rejected_body_is_retried(monkeypatch):
    """A refusal wearing an answer's clothes: HTTP 200 with an HTML error page."""
    _curl_returning(monkeypatch, _Result(0, "<html>429</html>"),
                    _Result(0, "@article{a,}"))
    assert ra._curl_get("https://dblp.org/x",
                        accept=lambda b: not b.startswith("<")) == "@article{a,}"


def test_curl_raising_is_a_failure_not_a_crash(monkeypatch):
    """subprocess.run(timeout=...) raises rather than returning non-zero, and an
    unhandled TimeoutExpired took down the whole run."""
    _curl_returning(monkeypatch, ra.subprocess.TimeoutExpired("curl", 30))
    assert ra._curl_get("https://dblp.org/x") is None
    assert ra.unanswered_lookups() == 1


def test_the_host_is_named_when_a_request_goes_unanswered(monkeypatch, capsys):
    _curl_returning(monkeypatch, _Result(7, ""))
    ra._curl_get("https://dblp.org/search/publ/api?q=x")
    assert "dblp.org" in capsys.readouterr().out


# ── DBLP ─────────────────────────────────────────────────────────────────────

def test_dblp_results_are_split_into_entries(monkeypatch):
    curl(monkeypatch, {"dblp.org": "@article{a,\n title={A}\n}\n"
                                   "@inproceedings{b,\n title={B}\n}\n"})
    entries = ra.search_dblp("A Paper")
    assert len(entries) == 2
    assert entries[0].startswith("@article{a,")


def test_an_html_error_page_from_dblp_is_no_answer_not_no_results(monkeypatch):
    """DBLP answers rate limiting with an HTML page and HTTP 200.

    The distinction this asserts is the whole bug: read as an empty result list,
    the refusal means "DBLP has no published version of this paper", the resolver
    falls through to the arXiv preprint, and a paper that came out at ACL 2026
    goes back to being cited as a preprint with nothing logged.
    """
    curl(monkeypatch, {"dblp.org": "<html><body>429</body></html>"})
    assert ra.search_dblp("A Paper") is None
    assert ra.unanswered_lookups() == 1


def test_an_empty_dblp_response_is_no_results(monkeypatch):
    """DBLP answers a no-hit title search with an empty body and HTTP 200, so
    empty really does mean "not in DBLP" -- and must not be retried or counted."""
    curl(monkeypatch, {})
    assert ra.search_dblp("A Paper") == []
    assert ra.unanswered_lookups() == 0


def test_the_title_is_url_encoded(monkeypatch):
    """An unencoded `&` or `?` truncates the query, so DBLP searches for a
    prefix of the title and confidently returns a different paper."""
    seen = []
    monkeypatch.setattr(ra, "_curl_get",
                        lambda url, **kw: seen.append(url) or "")
    ra.search_dblp("Cause & Effect: What?")
    assert "Cause%20%26%20Effect" in seen[0]


# ── Semantic Scholar title search ────────────────────────────────────────────

def test_s2_title_search_prefers_a_candidate_from_the_right_year(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {"data": [
        {"title": "Wrong", "year": 2015},
        {"title": "Right", "year": 2024}]})
    assert ra.query_s2_by_title("A Paper", "2024")["title"] == "Right"


def test_a_one_year_gap_is_still_the_same_paper(monkeypatch):
    """A preprint and its publication are usually a year apart."""
    monkeypatch.setattr(ra, "_http_get_json",
                        lambda url: {"data": [{"title": "T", "year": 2023}]})
    assert ra.query_s2_by_title("A Paper", "2024")["title"] == "T"


def test_a_candidate_with_no_year_is_not_rejected(monkeypatch):
    """S2 records for very recent papers often have a null year; dropping them
    would lose exactly the papers most likely to be unresolved."""
    monkeypatch.setattr(ra, "_http_get_json",
                        lambda url: {"data": [{"title": "T", "year": None}]})
    assert ra.query_s2_by_title("A Paper", "2024") is not None


def test_the_first_result_is_used_when_no_year_is_known(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json",
                        lambda url: {"data": [{"title": "First"}, {"title": "Second"}]})
    assert ra.query_s2_by_title("A Paper")["title"] == "First"


def test_every_candidate_from_the_wrong_year_falls_back_to_the_best_guess(monkeypatch):
    """Rather than nothing: the caller only uses S2 for its identifier
    crosswalk, and a wrong ID is caught downstream by the title guards."""
    monkeypatch.setattr(ra, "_http_get_json",
                        lambda url: {"data": [{"title": "Old", "year": 1999}]})
    assert ra.query_s2_by_title("A Paper", "2024")["title"] == "Old"


@pytest.mark.parametrize("payload", [None, {}, {"data": []}])
def test_an_empty_s2_response_is_no_match(monkeypatch, payload):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: payload)
    assert ra.query_s2_by_title("A Paper") is None


def test_s2_by_arxiv_asks_for_the_fields_the_crosswalk_needs(monkeypatch):
    seen = []
    monkeypatch.setattr(ra, "_http_get_json", lambda url: seen.append(url) or {})
    ra.query_s2_by_arxiv("2401.00001")
    assert "arXiv:2401.00001" in seen[0]
    assert "externalIds" in seen[0]


# ── ACL Anthology ────────────────────────────────────────────────────────────

def test_an_acl_entry_is_refiled_under_our_key(monkeypatch):
    curl(monkeypatch, {"aclanthology.org":
                       "@inproceedings{their-key,\n  title = {A Paper}\n}\n"})
    bib = ra.fetch_acl_bib("2024.acl-long.1", "ours2024paper")
    assert bib.startswith("@inproceedings{ours2024paper,")
    assert "their-key" not in bib


def test_an_acl_404_page_is_rejected(monkeypatch):
    """The Anthology serves a 404 body rather than an error for an ID it does not
    have, and S2's ACL IDs are sometimes wrong."""
    curl(monkeypatch, {"aclanthology.org": "<!DOCTYPE html><html>Not found"})
    assert ra.fetch_acl_bib("nope", "k") is None


def test_an_empty_acl_response_is_rejected(monkeypatch):
    curl(monkeypatch, {})
    assert ra.fetch_acl_bib("nope", "k") is None


# ── OpenReview ───────────────────────────────────────────────────────────────

_OR_NOTE = {
    "cdate": 1700000000000,
    "content": {
        "title": {"value": "A Reviewed Paper"},
        "authors": {"value": ["Ada Lovelace", "Alan Turing"]},
        "venue": {"value": "ICLR 2024 Poster"},
    },
}


def test_an_openreview_note_becomes_a_published_entry(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: _OR_NOTE)
    bib = ra.fetch_openreview_bib("AbC123", "k1")
    assert bib.startswith("@inproceedings{k1,")
    assert "title = {A Reviewed Paper}" in bib
    assert "author = {Ada Lovelace and Alan Turing}" in bib
    assert "booktitle = {ICLR 2024 Poster}" in bib
    assert "url = {https://openreview.net/forum?id=AbC123}" in bib


def test_the_api2_value_wrapper_is_unwrapped(monkeypatch):
    """api2 wraps every field as {"value": ...} while api1 did not. Read
    literally, the title becomes the string "{'value': 'A Paper'}"."""
    monkeypatch.setattr(ra, "_http_get_json", lambda url: _OR_NOTE)
    assert "value" not in ra.fetch_openreview_bib("f", "k1")


def test_a_plain_field_is_also_accepted(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {
        "content": {"title": "Plain", "authors": "One Author", "venue": "V"}})
    bib = ra.fetch_openreview_bib("f", "k1")
    assert "title = {Plain}" in bib and "author = {One Author}" in bib


def test_the_year_comes_from_the_creation_timestamp(monkeypatch):
    """cdate is epoch milliseconds. Taking its first four digits as the year --
    which is what this did -- dates every OpenReview paper to 1700."""
    monkeypatch.setattr(ra, "_http_get_json", lambda url: _OR_NOTE)
    assert "year = {2023}" in ra.fetch_openreview_bib("f", "k1")


def test_a_note_with_no_cdate_falls_back_to_its_year_field(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {
        "content": {"title": {"value": "T"}, "year": {"value": "2022"}}})
    assert "year = {2022}" in ra.fetch_openreview_bib("f", "k1")


@pytest.mark.parametrize("cdate", [None, "", "not-a-number"])
def test_an_unusable_cdate_leaves_the_year_empty(monkeypatch, cdate):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {
        "cdate": cdate, "content": {"title": {"value": "T"}}})
    bib = ra.fetch_openreview_bib("f", "k1")
    assert "year = {}" in bib
    assert is_wellformed_entry(bib, expected_key="k1")


def test_venueid_stands_in_for_a_missing_venue(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {
        "cdate": 1700000000000,
        "content": {"title": {"value": "T"}, "venueid": {"value": "ICLR.cc/2024"}}})
    assert "booktitle = {ICLR.cc/2024}" in ra.fetch_openreview_bib("f", "k1")


def test_a_note_with_no_title_is_not_an_entry(monkeypatch):
    """A titleless entry would be appended to orig.bib and then never match any
    row, so it would be re-resolved forever."""
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {"content": {}})
    assert ra.fetch_openreview_bib("f", "k1") is None


def test_an_unreachable_openreview_is_not_an_entry(monkeypatch):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: None)
    assert ra.fetch_openreview_bib("f", "k1") is None


def test_a_brace_in_an_openreview_title_is_escaped(monkeypatch):
    """The values are JSON, so they are plain text: an unbalanced brace written
    verbatim breaks not just this entry but the rest of the file."""
    monkeypatch.setattr(ra, "_http_get_json", lambda url: {
        "cdate": 1700000000000,
        "content": {"title": {"value": "A } Paper"}, "venue": {"value": "V"}}})
    bib = ra.fetch_openreview_bib("f", "k1")
    assert is_wellformed_entry(bib, expected_key="k1")


@pytest.mark.parametrize("forum_id", ["AbC-123_x", "zZ9"])
def test_the_forum_id_round_trips_through_the_url(monkeypatch, forum_id):
    monkeypatch.setattr(ra, "_http_get_json", lambda url: _OR_NOTE)
    bib = ra.fetch_openreview_bib(forum_id, "k1")
    assert f"id={forum_id}" in bib
    assert ra._extract_openreview_id(bib) == forum_id


# ── OpenAlex ─────────────────────────────────────────────────────────────────

def _work(**over):
    work = {
        "title": "A Long Enough Paper Title",
        "type": "article",
        "publication_year": 2024,
        "doi": "https://doi.org/10.1234/abcd",
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "primary_location": {"source": {"display_name": "Nature"}},
        "biblio": {},
    }
    work.update(over)
    return work


def _results(*works):
    return json.dumps({"results": list(works)})


def test_openalex_returns_the_matching_work(monkeypatch):
    curl(monkeypatch, {"api.openalex.org": _results(_work())})
    assert ra.search_openalex("A Long Enough Paper Title")["title"] == \
        "A Long Enough Paper Title"


def test_an_unrelated_openalex_result_is_rejected(monkeypatch):
    """The failure this exists for: a query about "MuLER: Detailed and Scalable
    Reference-based Evaluation" came back as "Regional climate modeling on
    European scales", with no error."""
    curl(monkeypatch, {"api.openalex.org":
                       _results(_work(title="Regional Climate Modeling on European Scales"))})
    assert ra.search_openalex("MuLER Detailed and Scalable Reference-based Evaluation") is None


def test_a_short_title_is_never_searched(monkeypatch):
    """Too little to reject a wrong answer with, and the answer would then
    overwrite a good entry."""
    monkeypatch.setattr(ra, "_curl_get",
                        lambda url: pytest.fail("searched on a 3-character title"))
    assert ra.search_openalex("NLP") is None


def test_the_broad_search_is_tried_when_the_phrase_filter_finds_nothing(monkeypatch):
    """Two different queries, not a retry: `filter=title.search:` is a phrase
    filter and `search` is fuzzy, and each finds papers the other misses."""
    urls = []

    def _get(url):
        urls.append(url)
        return _results(_work()) if "search=" in url else _results()
    monkeypatch.setattr(ra, "_curl_get", _get)
    assert ra.search_openalex("A Long Enough Paper Title") is not None
    assert len(urls) == 2
    assert "title.search:" in urls[0] and "search=" in urls[1]


def test_malformed_openalex_json_is_no_result(monkeypatch):
    curl(monkeypatch, {"api.openalex.org": "<html>gateway timeout"})
    assert ra.search_openalex("A Long Enough Paper Title") is None


def test_an_empty_openalex_body_is_no_result(monkeypatch):
    curl(monkeypatch, {})
    assert ra.search_openalex("A Long Enough Paper Title") is None


def test_a_json_body_that_is_not_an_object_is_no_result(monkeypatch):
    curl(monkeypatch, {"api.openalex.org": "null"})
    assert ra.search_openalex("A Long Enough Paper Title") is None


def test_the_contact_email_is_sent_when_configured(monkeypatch):
    """OpenAlex asks callers to identify themselves, and puts unidentified
    traffic in a slower pool."""
    import config
    urls = []
    monkeypatch.setattr(config, "CONTACT_EMAIL", "a@example.com", raising=False)
    monkeypatch.setattr(ra, "_curl_get", lambda url: urls.append(url) or _results())
    ra.search_openalex("A Long Enough Paper Title")
    assert "mailto=a%40example.com" in urls[0]


# ── OpenAlex -> BibTeX ───────────────────────────────────────────────────────

def test_a_journal_article_becomes_an_article_entry():
    bib, published = ra.openalex_to_bibtex(_work(), "k1")
    assert bib.startswith("@article{k1,")
    assert "journal = {Nature}" in bib
    assert "author = {Ada Lovelace}" in bib
    assert "year = {2024}" in bib
    assert "doi = {10.1234/abcd}" in bib, "the https prefix must be stripped"
    assert published is True


def test_a_conference_paper_becomes_inproceedings():
    bib, published = ra.openalex_to_bibtex(
        _work(type="proceedings-article",
              primary_location={"source": {"display_name": "ACL"}}), "k1")
    assert bib.startswith("@inproceedings{k1,")
    assert "booktitle = {ACL}" in bib
    assert published is True


@pytest.mark.parametrize("over", [
    {"type": "preprint"},
    {"type": "posted-content"},
    {"primary_location": {"source": {"display_name": "arXiv (Cornell University)"}}},
    {"doi": "https://doi.org/10.48550/arxiv.2401.00001"},
])
def test_a_preprint_is_labelled_as_one(over):
    """The label is the whole point: only a published result may replace an
    existing entry, so a preprint that claimed to be published would downgrade
    a paper's own @inproceedings back to a @misc."""
    bib, published = ra.openalex_to_bibtex(_work(**over), "k1")
    assert published is False
    assert bib.startswith("@misc{k1,")
    assert "journal" not in bib and "booktitle" not in bib


def test_a_work_with_no_venue_is_a_misc_entry():
    bib, published = ra.openalex_to_bibtex(_work(primary_location={}), "k1")
    assert bib.startswith("@misc{k1,")
    assert published is True


def test_volume_issue_and_pages_are_carried_over():
    bib, _ = ra.openalex_to_bibtex(_work(biblio={
        "volume": "12", "issue": "3", "first_page": "1", "last_page": "9"}), "k1")
    assert "volume = {12}" in bib
    assert "number = {3}" in bib, "OpenAlex calls it issue; BibTeX calls it number"
    assert "pages = {1--9}" in bib


def test_a_single_page_needs_no_range():
    bib, _ = ra.openalex_to_bibtex(_work(biblio={"first_page": "7"}), "k1")
    assert "pages = {7}" in bib


def test_a_titleless_work_produces_nothing():
    assert ra.openalex_to_bibtex(_work(title=None), "k1") == (None, False)


def test_authorships_without_a_name_are_skipped():
    bib, _ = ra.openalex_to_bibtex(_work(authorships=[
        {"author": {"display_name": "Ada Lovelace"}},
        {"author": None},
        {}]), "k1")
    assert "author = {Ada Lovelace}" in bib


def test_a_brace_in_an_openalex_title_is_escaped():
    bib, _ = ra.openalex_to_bibtex(_work(title="A } Paper"), "k1")
    assert is_wellformed_entry(bib, expected_key="k1")


def test_every_openalex_entry_parses_back():
    for over in ({}, {"type": "preprint"}, {"primary_location": {}},
                 {"biblio": {"volume": "1", "first_page": "2", "last_page": "3"}}):
        bib, _ = ra.openalex_to_bibtex(_work(**over), "k1")
        assert is_wellformed_entry(bib, expected_key="k1"), over


# ── the arXiv fallback ───────────────────────────────────────────────────────

_ABS_PAGE = """
<html><body>
<h1 class="title">Title:A Preprint About Things</h1>
<div class="authors"><a href="/a/x">Ada Lovelace</a>, <a href="/a/y">Alan Turing</a></div>
<div class="submission-history">Submitted on 3 January 2024</div>
</body></html>
"""


def test_the_abstract_page_is_parsed_into_an_entry(monkeypatch):
    curl(monkeypatch, {"arxiv.org/abs": _ABS_PAGE})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1")
    assert "title = {A Preprint About Things}" in bib, "the Title: prefix must go"
    assert "author = {Ada Lovelace and Alan Turing}" in bib
    assert "year = {2024}" in bib
    assert "eprint = {2401.00001}" in bib


def test_an_unreachable_arxiv_still_yields_an_entry(monkeypatch):
    """This is the last fallback. Returning nothing here means the paper has no
    entry at all, so it is dropped from the CV rather than listed as a preprint."""
    curl(monkeypatch, {})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1")
    assert bib.startswith("@misc{k1,")
    assert "eprint = {2401.00001}" in bib


def test_the_known_title_survives_an_unreachable_arxiv(monkeypatch):
    """The caller already has the title, from the table or the existing entry.
    Dropping it made the fallback entry titleless, which update_bib_inplace then
    refuses -- so a paper arXiv happened to rate-limit was left out of the CV and
    retried on every subsequent run, with the title known the whole time."""
    curl(monkeypatch, {})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1", known_title="A Known Paper")
    assert "title = {A Known Paper}" in bib
    assert is_wellformed_entry(bib, expected_key="k1"), (
        "a titleless entry is refused by update_bib_inplace")


def test_a_captcha_page_also_keeps_the_known_title(monkeypatch):
    curl(monkeypatch, {"arxiv.org/abs": "<html><body>captcha</body></html>"})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1", known_title="A Known Paper")
    assert is_wellformed_entry(bib, expected_key="k1")


def test_resolve_hands_the_title_to_the_fallback(monkeypatch):
    """The end-to-end version of the same failure."""
    monkeypatch.setattr(ra, "search_dblp", lambda t: [])
    monkeypatch.setattr(ra, "query_s2_by_arxiv", lambda i: None)
    monkeypatch.setattr(ra, "search_openalex", lambda t: None)
    monkeypatch.setattr(ra, "_clibib_fetch", lambda: None)
    monkeypatch.setattr(ra, "_curl_get", lambda url: "")
    bib, source = ra.resolve("A Paper With A Long Enough Title", "2401.00001", "k1")
    assert source == "arXiv (export API)"
    assert "title = {A Paper With A Long Enough Title}" in bib
    assert is_wellformed_entry(bib, expected_key="k1")


def test_a_brace_in_the_known_title_is_escaped():
    bib = ra._bare_arxiv_bib("2401.00001", "k1", "A } Paper")
    assert is_wellformed_entry(bib, expected_key="k1")


def test_a_page_with_no_title_falls_back_to_the_bare_entry(monkeypatch):
    """arXiv serves a CAPTCHA to a run it thinks is scraping. With no title from
    the page and none from the caller, the entry says so rather than inventing
    one -- and is then refused downstream instead of entering the CV blank."""
    curl(monkeypatch, {"arxiv.org/abs": "<html><body>captcha</body></html>"})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1")
    assert extract_field(bib, "title") == ""
    assert not is_wellformed_entry(bib, expected_key="k1")


def test_a_page_with_no_author_block_is_survivable(monkeypatch):
    curl(monkeypatch, {"arxiv.org/abs":
                       '<html><h1 class="title">Title:A Paper</h1></html>'})
    bib = ra.fetch_arxiv_bib("2401.00001", "k1")
    assert "author = {}" in bib
    assert is_wellformed_entry(bib, expected_key="k1")


def test_the_bare_entry_infers_the_year_from_the_arxiv_id():
    """2401.00001 is January 2024: the ID's first two digits are the year."""
    assert "year = {2024}" in ra._bare_arxiv_bib("2401.00001", "k1")


def test_an_unparseable_arxiv_id_leaves_the_year_blank():
    """An old-style ID (math/0601001) carries no year in a form this can read.
    A blank is honest; a guessed year would be printed in the CV."""
    bib = ra._bare_arxiv_bib("math/0601001", "k1", "A Paper")
    assert "year = {}" in bib
    assert is_wellformed_entry(bib, expected_key="k1")


def test_every_arxiv_fallback_entry_parses_back(monkeypatch):
    curl(monkeypatch, {"arxiv.org/abs": _ABS_PAGE})
    assert is_wellformed_entry(ra.fetch_arxiv_bib("2401.00001", "k1"),
                                  expected_key="k1")


# ── the CLI ──────────────────────────────────────────────────────────────────

_EXISTING = """@misc{k1,
  title = {A Paper},
  eprint = {2401.00001},
  archivePrefix = {arXiv},
  year = {2024}
}
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Drive main() with every source stubbed and every path inside tmp_path."""
    monkeypatch.setattr(ra, "ATTEMPTS_PATH", str(tmp_path / "attempts.json"))
    monkeypatch.setattr(ra, "get_missing_bib_entries", lambda text, df=None: [])
    monkeypatch.setattr(ra, "resolve",
                        lambda title, arxiv_id, key, content="", store=None: (
                            f"@inproceedings{{{key},\n  title = {{{title}}},\n"
                            f"  booktitle = {{ACL}},\n  year = {{2024}}\n}}", "DBLP"))
    bib = tmp_path / "orig.bib"
    bib.write_text(_EXISTING)
    return tmp_path, str(bib), str(tmp_path / "resolved.bib")


def test_the_cli_writes_what_it_resolved(cli, capsys):
    _tmp, bib, out = cli
    ra.main(["--bib", bib, "--output", out])
    assert "booktitle = {ACL}" in open(out).read()
    assert "1/1 entries written" in capsys.readouterr().out


def test_the_cli_leaves_the_bib_alone_without_in_place(cli, capsys):
    """An upgrade rewrites a hand-curated file, so it is opt-in and the run says
    how to ask for it."""
    _tmp, bib, out = cli
    ra.main(["--bib", bib, "--output", out])
    assert open(bib).read() == _EXISTING
    assert "rerun with --in-place" in capsys.readouterr().out


def test_in_place_upgrades_the_preprint_entry(cli, capsys):
    _tmp, bib, out = cli
    ra.main(["--bib", bib, "--output", out, "--in-place"])
    text = open(bib).read()
    assert extract_field(text, "booktitle") == "ACL"
    assert "1 entries upgraded in place" in capsys.readouterr().out
    assert extract_field(text, "title") == "A Paper", (
        "the curated title is not the source's to change")


def test_skip_missing_does_not_read_the_table(cli, monkeypatch):
    _tmp, bib, out = cli
    monkeypatch.setattr(ra, "get_missing_bib_entries",
                        lambda *a, **k: pytest.fail("read the table under --skip-missing"))
    ra.main(["--bib", bib, "--output", out, "--skip-missing"])


def test_attempts_are_recorded_even_for_a_failed_lookup(cli, monkeypatch):
    """The count is what deprioritizes hopeless entries and what WORKLIST.md
    shows, so it must be written per entry rather than at the end -- an
    interrupted run would otherwise learn nothing."""
    _tmp, bib, out = cli
    monkeypatch.setattr(ra, "resolve",
                        lambda *a, **k: ("", "not found"))
    ra.main(["--bib", bib, "--output", out])
    assert ra.load_attempts() == {"k1": 1}


def test_a_candidate_is_not_resolved_twice(cli, monkeypatch):
    """An arXiv entry that is also a table row with no key appears in both
    lists."""
    _tmp, bib, out = cli
    calls = []
    monkeypatch.setattr(ra, "get_missing_bib_entries", lambda text, df=None: [
        {"item_name": "k1", "title": "A Paper", "content": ""}])
    monkeypatch.setattr(ra, "resolve", lambda title, arxiv_id, key, content="",
                        store=None: calls.append(key) or ("", "not found"))
    ra.main(["--bib", bib, "--output", out])
    assert calls == ["k1"]


def test_the_deprioritized_count_is_reported(cli, capsys):
    _tmp, bib, out = cli
    ra.save_attempts({"k1": ra._DEPRIORITIZE_AFTER + 1})
    ra.main(["--bib", bib, "--output", out])
    assert f"≥{ra._DEPRIORITIZE_AFTER} prior attempts sorted last" in capsys.readouterr().out


def test_an_empty_bib_produces_an_empty_output(cli):
    tmp, _bib, out = cli
    empty = tmp / "empty.bib"
    empty.write_text("")
    ra.main(["--bib", str(empty), "--output", out])
    assert open(out).read() == "", "a fresh fork has no entries and must not crash"
