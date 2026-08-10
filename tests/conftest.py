"""Suite-wide guarantees, applied to every test whether it asks or not.

Two of them. The first: no test may write the repository's own data files. The
second: no test may read the real Semantic Scholar key.

The write guard exists because a test did. A stubbed `IdentityStore.load` still
hands back a store that saves to the default path, so one `dedupe.main()` run
under test replaced 1139 lines of harvested crosswalk with the two keys of a
fixture -- silently, in a passing suite, and only visible in `git status`. The
same shape is available to any test through `write_table`, `Venues.save`,
`save_attempts` or `write_citation_rows`: stub the read, forget the write, and the
suite edits the author's papers.csv while reporting green.

The key guard is not only about isolation. The key now lives in a file the pipeline reads
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

# Everything at the repository root the pipeline writes on its own. A fork has
# fewer of them, and a missing file is a state worth restoring too: a test that
# creates papers.csv where there was none leaves the next run reading a fixture.
_PIPELINE_DATA = (
    "papers.csv",
    "orig.bib",
    "citations.csv",
    "identity.json",
    "resolve_attempts.json",
    "venues.yaml",
    "profile_stats.json",
    "WORKLIST.md",
    ".pipeline_state.json",
)


def _read(name):
    try:
        with open(os.path.join(ROOT, name), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


@pytest.fixture(scope="session")
def _pipeline_data():
    """The contents of the pipeline's own files as the suite found them."""
    return {name: _read(name) for name in _PIPELINE_DATA}


@pytest.fixture(autouse=True)
def _pipeline_data_untouched(_pipeline_data):
    """Fail any test that writes one of those files, and put it back.

    Restoring rather than only reporting, for two reasons: the damage is to real
    data the rest of the suite then reads, so one careless test would otherwise
    cascade into unrelated failures; and the author's working tree is not a
    scratch space, even when git could recover it.
    """
    yield
    damaged = []
    for name, before in _pipeline_data.items():
        if _read(name) == before:
            continue
        damaged.append(name)
        path = os.path.join(ROOT, name)
        if before is None:
            os.remove(path)
        else:
            with open(path, "wb") as f:
                f.write(before)
    if damaged:
        pytest.fail(
            f"wrote the repository's own {', '.join(damaged)} (restored). Stub the "
            f"save as well as the load, or point the module's path constant at "
            f"tmp_path.")


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
