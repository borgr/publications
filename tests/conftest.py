"""Suite-wide guarantees, applied to every test whether it asks or not.

Right now there is one: no test may read the real Semantic Scholar key.

That is not only about isolation. The key now lives in a file the pipeline reads
on its own, outside the repository, so `s2_api_key()` finds it on the author's
machine and finds nothing in CI -- which makes any behaviour that depends on it
pass in one place and fail in the other, for a reason no failure message would
mention. Blanking it here makes the unauthenticated path the default everywhere,
and a test that wants a key sets one explicitly.

The other half is that a credential should not be reachable from a test at all. A
failing assertion prints what it compared, and pytest prints local variables on
error, so a suite that can see the key is a suite that can put it in a log.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _no_real_api_key(tmp_path, monkeypatch):
    """Point all three key sources at nothing: environment, file, config.py."""
    monkeypatch.delenv("S2_API_KEY", raising=False)
    import resolve_arxiv
    absent = str(tmp_path / "no-such-key-file")
    monkeypatch.setattr(resolve_arxiv, "KEY_FILE", absent)
    monkeypatch.setattr(resolve_arxiv, "FILE_SOURCE", absent)
    import config
    monkeypatch.setattr(config, "S2_API_KEY", "", raising=False)


@pytest.fixture(autouse=True)
def _fresh_network_state(monkeypatch):
    """Per-host pacing, per-host cooldowns, prefetched S2 answers, and the count of
    lookups that went missing all live in module globals, which outlive a test.

    Left alone, a test that trips a cooldown makes every later test see a paused
    source and pass without making a request; a prefetched answer makes a later
    test's lookup succeed without one; and a leftover count makes a test think its
    own lookup went missing. All the same failure: a test that passes, or fails,
    because of something an earlier test did.
    """
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "_host_state", {})
    monkeypatch.setattr(resolve_arxiv, "_s2_batch", {})
    monkeypatch.setitem(resolve_arxiv._net_state, "unanswered", 0)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The two doors out of resolve_arxiv, wired to fail the test that uses one.

    Suite-wide rather than per-file, because per-file is how two tests in
    test_resolve.py came to fetch api.openalex.org for real: they stubbed the rungs
    of the ladder they were interested in and let the rest fall through. Nothing
    failed, so nothing said so -- the suite just quietly needed a network, and
    would have failed on a plane, behind a proxy, or in a fork's CI, for reasons
    having nothing to do with the code under test.

    A test that wants a source stubs it; a test of _curl_get itself replaces
    subprocess.run underneath it, which is a door this does not hold shut.
    """
    import resolve_arxiv
    monkeypatch.setattr(resolve_arxiv, "_curl_get",
                        lambda url, **kw: pytest.fail(f"unstubbed network call: {url}"))
    monkeypatch.setattr(resolve_arxiv, "urlopen",
                        lambda req, **kw: pytest.fail(f"unstubbed network call: {req}"))
    monkeypatch.setattr(resolve_arxiv.time, "sleep", lambda _s: None)
