#!/usr/bin/env python3
"""One-command update for the publications pipeline.

Steps. Each is skipped when the *contents* of its inputs are unchanged since
that step last succeeded (recorded in .pipeline_state.json), so re-running is
cheap and safe:

  1. Refresh citations.csv from Google Scholar (time-based: --fetch-age)
  2. Add papers that are in Scholar but not yet in the publications table
  3. Resolve arXiv entries in orig.bib to their published BibTeX, and resolve
     table rows that have no entry yet
  4. Build overleaf/Wzmn.bib from orig.bib + the table's venue/tag metadata
  5. Rebuild overleaf/main.tex: \\nocite{} blocks, citation total, h-index
  6. Regenerate WORKLIST.md -- everything the pipeline cannot decide itself
  7. Commit and push to GitHub and Overleaf, rebasing if a remote has moved

Exits non-zero if anything failed, and notifies, so an unattended run cannot
fail silently while the CV keeps looking current.

Usage:
    python update.py [--dry-run] [--force] [--no-push] [--no-notify]
                     [--skip-fetch] [--skip-new] [--skip-resolve] [--skip-publications]
                     [--skip-tex] [--fetch-age HOURS] [--user SCHOLAR_USER_ID]
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FILE_DIR)

import notify
import overleaf_auth
from bib_edit import (
    _PUBLISHED_SOURCES,
    _get_arxiv_id,
    get_arxiv_entries,
    get_missing_bib_entries,
    update_bib_inplace,
)
from bib_utils import extract_field, parse_bibtex
from build_bib import simplify_venue
from citations_io import read_citation_rows
from identity import (
    MATCH_NORMALIZED,
    IdentityStore,
    classify_title,
    join_citations,
    normalize_title,
)
from pipeline_state import AlreadyRunning, PipelineState, RunLock
from resolve_arxiv import (
    _DEPRIORITIZE_AFTER,
    UNANSWERED,
    load_attempts,
    prefetch_s2_by_arxiv,
    reset_unanswered_lookups,
    resolve,
    save_attempts,
    sort_by_attempts,
    unanswered_lookups,
)
from table_io import (
    CSV_PATH,
    append_rows,
    fill_blanks,
    read_table,
    set_bib_keys,
    set_column,
)
from venues import Venues

CITATIONS_CSV = os.path.join(FILE_DIR, "citations.csv")
TABLE_PATH    = CSV_PATH
BIB_PATH      = os.path.join(FILE_DIR, "orig.bib")
STATS_PATH    = os.path.join(FILE_DIR, "profile_stats.json")
VENUES_PATH   = os.path.join(FILE_DIR, "venues.yaml")
OVERLEAF_DIR  = os.path.join(FILE_DIR, "overleaf")
WZMN_BIB      = os.path.join(OVERLEAF_DIR, "Wzmn.bib")
WORKLIST_PATH = os.path.join(FILE_DIR, "WORKLIST.md")
TEX_PATH      = os.path.join(OVERLEAF_DIR, "main.tex")

# Columns are addressed by name via table_io; the positional COL_* constants
# that used to live here (COL_PAPER = 26, alongside `row_data = [None] * 37`)
# are gone, along with the class of bug where inserting a column misfiled data.


# ── Helpers ────────────────────────────────────────────────────────────────────

# Each step's inputs. A step re-runs when any of its inputs' *contents* differ
# from the last successful run. Three of these entries fix real skip bugs: step 4
# ignored citations.csv, so refreshed counts never reached the bibliography; step
# 5 ignored profile_stats.json, so a new h-index never reached the CV; and step 3
# ignored orig.bib, which it both reads and writes, so a preprint entry added or
# corrected by hand was never looked up until something else happened to change.
STEP_INPUTS = {
    "resolve":      [BIB_PATH, TABLE_PATH, CITATIONS_CSV],
    "build_bib":    [BIB_PATH, TABLE_PATH, CITATIONS_CSV, VENUES_PATH],
    "rebuild_tex":  [WZMN_BIB, STATS_PATH],
}

# What each step produces. Also checked before skipping, because a step whose
# inputs are unchanged has still not "already run" if its output was since edited,
# reverted or deleted. Without this, hand-reverting the citation totals in
# main.tex was permanent: step 5's inputs were untouched, so it auto-skipped on
# every subsequent run and only CI ever noticed the CV had gone stale.
# Each file is listed under the one step that owns it; step 5 also rewrites
# Wzmn.bib on its way through, but step 4 is what guards that.
STEP_OUTPUTS = {
    "resolve":      [BIB_PATH],
    "build_bib":    [WZMN_BIB],
    "rebuild_tex":  [TEX_PATH],
}




def check_push_credential(enabled: bool = True) -> str | None:
    """Report early that step 7 will not be able to push, without stopping the run.

    Early, because the alternative is finding out after a Scholar fetch and six
    steps. Not blocking, because the six steps are worth running anyway: they leave
    papers.csv, citations.csv and the rebuilt CV current on disk and committed
    locally, and only the Overleaf push is lost. Overleaf tokens get revoked and
    rotated, and a revoked one that froze the whole pipeline would quietly stop the
    data tracking reality as well -- a much worse failure than a stale Overleaf.

    Step 7 still fails, notifies and exits non-zero on its own. This only makes the
    reason visible at the top of the log rather than at the bottom.
    """
    if not enabled:
        return None
    import rebuild_tex
    if rebuild_tex.check_overleaf_present():
        return None  # No submodule; preflight already blocks on that.
    return overleaf_auth.check_credential(OVERLEAF_DIR)


def describe_s2_key() -> str:
    """One line saying whether step 3 has a Semantic Scholar key, and from where.

    Not blocking, and not a problem: the pipeline works without one. It is printed
    because the difference is otherwise invisible -- an absent key does not fail,
    it throttles, and a throttled S2 costs the ACL Anthology and OpenReview too,
    since both are reached through its externalIds. A run that resolved nothing for
    a whole quarter would look exactly like a run with nothing left to resolve.

    Saying where the key came from is the point of it. Filling in a key that some
    other source shadows is silent, and so is exporting one in a shell the weekly
    run never sees.
    """
    import resolve_arxiv
    _key, source = resolve_arxiv.s2_api_key_source()
    if source:
        return f"Semantic Scholar: using the key from {source}."
    return ("Semantic Scholar: no API key, so step 3 will be throttled and may "
            f"resolve less. Put one in {resolve_arxiv.KEY_FILE} -- see the README.")


def preflight() -> list:
    """Check the things whose absence would otherwise fail deep into a run.

    Cheap, and it turns a bare FileNotFoundError in step 5 into an instruction
    before step 1. Only reports what is genuinely required -- an unset optional
    API key is not a problem.

    Everything here is blocking, and nothing here touches the network. The push
    credential is checked separately, by `check_push_credential`: it is the one
    problem that must be reported early without stopping the run.
    """
    problems = []
    if not shutil.which("curl"):
        problems.append("curl is not on PATH. Every HTTP fetch uses it, because "
                        "Python's TLS fingerprint gets blocked by Scholar.")
    if not shutil.which("git"):
        problems.append("git is not on PATH, so step 7 cannot push.")
    if not os.path.exists(TABLE_PATH):
        problems.append(f"No publications table: expected "
                        f"{os.path.basename(CSV_PATH)}.")
    if not os.path.exists(BIB_PATH):
        problems.append("orig.bib is missing.")

    import rebuild_tex
    overleaf_problem = rebuild_tex.check_overleaf_present()
    if overleaf_problem:
        problems.append(overleaf_problem)

    try:
        import config
        if not getattr(config, "SCHOLAR_USER_ID", ""):
            problems.append("config.SCHOLAR_USER_ID is empty, so step 1 has no "
                            "profile to fetch.")
        if not getattr(config, "AUTHOR_NAME", ""):
            problems.append("config.AUTHOR_NAME is empty; the BST files and "
                            "main.tex are patched from it.")
    except Exception as exc:
        problems.append(f"config.py could not be imported: {exc}")
    return problems


# ── Step 1 ─────────────────────────────────────────────────────────────────────

def step1_fetch(dry_run: bool, user: str | None = None) -> None:
    if dry_run:
        print("  (dry-run: skipped)")
        return
    cmd = [sys.executable, os.path.join(FILE_DIR, "fetch_citations.py")]
    if user:
        cmd.append(user)
    subprocess.run(cmd, check=True)
    print("  Done.")


# ── Step 2 ─────────────────────────────────────────────────────────────────────

def step2_add_new_papers(dry_run: bool) -> int:
    """Return number of new papers added (or that would be added)."""
    print("\n[Step 2] Checking for new papers not in the publications table")
    df     = read_table()
    papers = read_citation_rows(CITATIONS_CSV)

    known_titles = [str(n) for n in df["Name"].dropna()]

    # A Scholar record whose ID is already bound to a paper is known outright,
    # regardless of what its title now says. Without this the fuzzy-matched
    # papers are re-reported on every run forever, because step 2 compared only
    # titles and never consulted the identifiers the pipeline had recorded.
    store = IdentityStore.load()
    bound_scholar_ids = set(store.index("scholar_id"))

    new_papers, assumed_known = [], []
    for p in papers:
        if "patent" in (p.get("venue") or "").lower():
            continue
        if p.get("scholar_id") and p["scholar_id"] in bound_scholar_ids:
            continue
        matched, tier, score = classify_title(p["title"], known_titles)
        if matched is None:
            p["_closest"] = (score, "")
            new_papers.append(p)
            continue
        # Judged already present. Report the ones decided on similarity rather
        # than on an exact normalized title, so "already known" is never a silent
        # verdict -- a wrong one loses the paper permanently.
        if tier != MATCH_NORMALIZED:
            assumed_known.append((p["title"], matched, score))

    if assumed_known:
        print(f"  {len(assumed_known)} Scholar paper(s) treated as already in the "
              f"table on title similarity (check these are not new papers):")
        for title, matched, score in sorted(assumed_known, key=lambda r: r[2]):
            print(f"    {score:.0%}  scholar: {title[:64]}")
            print(f"          table: {matched[:64]}")

    if not new_papers:
        print("  No new papers found.")
        return 0

    print(f"  {len(new_papers)} new paper(s):")
    for p in new_papers:
        year_val = int(p["year"]) if str(p["year"]).isdigit() else p["year"]
        score = p.get("_closest", (0.0, ""))[0]
        norm = normalize_title(p["title"])
        scored = sorted(
            ((difflib.SequenceMatcher(None, norm, normalize_title(t)).ratio(), t)
             for t in known_titles), reverse=True)
        hint = ""
        if scored and scored[0][0] > 0.60:
            hint = (f"\n        ↑ closest existing row: {scored[0][1][:60]!r} "
                    f"({scored[0][0]:.0%})")
        print(f"    [{year_val}] {p['title'][:70]}{hint}")

    if dry_run:
        print("  (dry-run: table not modified)")
        return len(new_papers)

    # Written by column name. The previous positional write built a 37-slot list
    # against hardcoded indices, so inserting a column silently misfiled values.
    rows = []
    for p in new_papers:
        rows.append({
            "Venue":   p.get("venue") or None,
            "Name":    p["title"],
            "Authors": p.get("authors") or None,
            "year":    int(p["year"]) if str(p["year"]).isdigit() else None,
            "Paper":   1,
        })
    added = append_rows(rows)
    print(f"  Added {added} new row(s) to {os.path.basename(TABLE_PATH)}")
    return added


def step2b_enrich(dry_run: bool) -> int:
    """Fill blank Authors and unusable Venue cells on existing rows from Scholar.

    These were recurring worklist items rather than one-off ones, because step 2
    creates them on every run: it copies Scholar's venue text verbatim (which
    never reduces to a venue key, so the paper gets no venueinf and is filed
    under ArXiv Articles), and a row with no Authors cannot have a BibTeX key
    generated for it, so step 3 files it under `unknown<year><title>`.

    Both are mechanical, so they are done here instead of being reported.
    Anything not mechanically decidable is left alone for the worklist.
    """
    print("\n[Step 2b] Filling gaps in existing rows from Scholar")
    df = read_table()
    rows = read_citation_rows(CITATIONS_CSV)
    if not rows:
        print("  No citation data to enrich from.")
        return 0

    join = join_citations(rows, sorted(set(df["Name"].dropna())),
                          store=IdentityStore.load())
    venues = Venues.load()
    # The bibliography, for resolving a venue from the entry rather than from
    # Scholar's truncated venue text.
    try:
        with open(BIB_PATH) as f:
            bib_entries = {e["item_name"]: e for e in parse_bibtex(f.read())}
    except OSError:
        bib_entries = {}

    author_fills, venue_fills = {}, {}

    # Authors come from Scholar, for rows Scholar knows about.
    for name, source in join.source.items():
        match = df[df["Name"] == name]
        if match.empty:
            continue
        authors = str(match.iloc[0].get("Authors") or "").strip()
        if authors.lower() in ("", "nan", "none") and source.get("authors"):
            author_fills[name] = source["authors"]

    # Venues come from the *bibliography*, which is authoritative: step 3 fetched
    # each entry from DBLP or the ACL Anthology, so its booktitle names the venue
    # in full where Scholar truncates ("Proceedings of the 29th International
    # Conference on Computational ..." could be COLING or Computational
    # Linguistics).
    #
    # Deliberately never from Scholar's venue string. A cell that does not resolve
    # is still a judgement -- "SURGeLLM" is a workshop, "ArXiv" means not yet
    # published -- and taking Scholar's word for it would have relabelled "The
    # mighty torr" (Workshop-paper=1) as an ACL main-conference paper. The bib
    # entry cannot make that mistake: a preprint's entry has no booktitle, so
    # there is nothing to fill from and the cell is left alone.
    for _, row in df.iterrows():
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        current = row.get("Venue")
        current_key = simplify_venue(current) if isinstance(current, str) else ""
        if current_key and venues.known(current_key):
            continue
        entry = bib_entries.get(str(row.get("Bib") or "").strip())
        if not entry:
            continue
        for field in ("booktitle", "journal"):
            value = extract_field(entry["content"], field)
            if not value:
                continue
            resolved = venues.match_raw(value)
            if resolved and resolved != current_key:
                venue_fills[name] = resolved
                break

    if not author_fills and not venue_fills:
        print("  Nothing to fill.")
        return 0

    for name, authors in sorted(author_fills.items()):
        print(f"  Authors  {name[:52]:<52} <- {authors[:34]}")
    for name, venue in sorted(venue_fills.items()):
        print(f"  Venue    {name[:52]:<52} <- {venue}")

    if dry_run:
        print("  (dry-run: table not modified)")
        return len(author_fills) + len(venue_fills)

    # Authors only where blank (Scholar is not more authoritative than a human);
    # venue overwritten, because the source is the bibliography itself.
    filled = fill_blanks({"Authors": author_fills})
    filled += set_column("Venue", venue_fills)
    print(f"  Filled {filled} cell(s) in {os.path.basename(TABLE_PATH)}")
    return filled


# ── Step 3 ─────────────────────────────────────────────────────────────────────

def step3_resolve(dry_run: bool) -> tuple:
    with open(BIB_PATH) as f:
        bib_text = f.read()

    attempts = load_attempts()
    # Identifiers seen during resolution are recorded here, so each paper is
    # matched by ID rather than by title similarity from the next run onwards.
    store = IdentityStore.load()

    # Part A: existing arXiv entries in orig.bib
    arxiv_entries = sort_by_attempts(get_arxiv_entries(bib_text), attempts)
    n_deprio = sum(1 for e in arxiv_entries if attempts.get(e["item_name"], 0) >= _DEPRIORITIZE_AFTER)
    print(f"  {len(arxiv_entries)} arXiv entries to check in orig.bib"
          + (f" ({n_deprio} with ≥{_DEPRIORITIZE_AFTER} prior attempts sorted last)" if n_deprio else "") + "...")

    if dry_run:
        # A dry run must not touch the network. Resolving every candidate takes
        # hundreds of requests and several minutes of deliberate rate-limit
        # sleeps, which is not what "show me what would change" should cost.
        missing = get_missing_bib_entries(bib_text)
        print("  (dry-run: no lookups performed)")
        print(f"  Would query {len(arxiv_entries)} arXiv entries for a published version")
        print(f"  Would look up {len(missing)} table row(s) with no entry in orig.bib:")
        for entry in missing[:20]:
            print(f"    [{entry['item_name']:<40}] {entry['title'][:60]}")
        if len(missing) > 20:
            print(f"    … and {len(missing) - 20} more")
        return 0, 0, len(arxiv_entries), []

    def _checkpoint():
        """Persist progress mid-loop.

        Resolving ~90 entries takes minutes of deliberate rate-limit sleeps. Both
        files used to be written only at the very end, so a crash or a Ctrl-C
        threw away every lookup the run had made and the next run repeated them.
        """
        save_attempts(attempts)
        store.save()

    # One request for every arXiv ID before resolving any of them, so that the
    # ladder's Semantic Scholar rung is answered from memory. This is the run the
    # batch exists for: on the first weekly run with an API key, S2 was asked
    # per paper, refused twice, went into cooldown, and 32 of the remaining 34
    # lookups were never attempted -- taking the ACL Anthology and OpenReview with
    # it, since both are reached through its externalIds.
    #
    # Part B below cannot be covered: those rows have no arXiv ID, which is why
    # they are looked up by title.
    n_prefetched = prefetch_s2_by_arxiv(_get_arxiv_id(e) for e in arxiv_entries)
    if n_prefetched:
        print(f"  Semantic Scholar answered for {n_prefetched} of them in one request")

    updates = []
    for i, entry in enumerate(arxiv_entries, 1):
        key      = entry["item_name"]
        arxiv_id = _get_arxiv_id(entry)
        label    = f"arXiv:{arxiv_id}" if arxiv_id else "(no arXiv ID)"
        print(f"    [{key[:40]}] {label:<22}", end=" ", flush=True)
        before = unanswered_lookups()
        bib, source = resolve(entry["title"], arxiv_id, key, entry.get("content", ""),
                              store=store)
        print(f"→ {source}")
        # Only a lookup that completed counts as an attempt. Counting a network
        # failure deprioritizes the paper for something that is not the paper's
        # fault, and one bad run is enough to push every unresolved entry past
        # _DEPRIORITIZE_AFTER at the same time.
        if bib or unanswered_lookups() == before:
            attempts[key] = attempts.get(key, 0) + 1
        updates.append((key, bib, source))
        if i % 10 == 0:
            _checkpoint()

    # Part B: table rows with no usable entry in orig.bib
    missing_entries = sort_by_attempts(get_missing_bib_entries(bib_text), attempts)
    new_entries = []
    not_found = []
    unanswered: list = []
    if missing_entries:
        print(f"\n  {len(missing_entries)} table row(s) with no BibTeX key...")
    for i, entry in enumerate(missing_entries, 1):
        key = entry["item_name"]
        print(f"    [{key:<40}]", end=" ", flush=True)
        before = unanswered_lookups()
        bib, source = resolve(entry["title"], None, key, "", store=store)
        print(f"→ {source}")
        if bib or unanswered_lookups() == before:
            attempts[key] = attempts.get(key, 0) + 1
        if bib:
            new_entries.append((key, bib))
        elif source == UNANSWERED:
            # Not a paper needing a hand-pasted entry; a question not yet asked.
            unanswered.append((entry["title"][:70], key))
        else:
            not_found.append((entry["title"][:70], key))
        if i % 10 == 0:
            _checkpoint()

    save_attempts(attempts)
    store.save()

    # Also track arXiv entries that couldn't be resolved to any bib at all
    for key, bib, source in updates:
        if not bib:
            title = next((e["title"] for e in arxiv_entries if e["item_name"] == key), key)
            bucket = unanswered if source == UNANSWERED else not_found
            bucket.append((title[:70], key))

    if unanswered:
        print(f"\n  {len(unanswered)} lookup(s) got no answer from any source they "
              f"needed. Not counted as failures; the next run asks again:")
        for title, key in unanswered[:10]:
            print(f"    [{key:<40}] {title}")
        if len(unanswered) > 10:
            print(f"    … and {len(unanswered) - 10} more")

    # Summary before writing
    upgraded = sum(1 for _, _, src in updates if src in _PUBLISHED_SOURCES)
    print(f"\n  {upgraded}/{len(arxiv_entries)} arXiv entries have a published version")
    print(f"  {len(new_entries)} new entries to append to orig.bib")

    n_still_arxiv = len(arxiv_entries) - upgraded
    new_bib_text, n_replaced, n_appended = update_bib_inplace(bib_text, updates, new_entries)

    if new_entries:
        # Write each resolved key back onto the row it came from. Taken straight
        # from `missing_entries`, which already pairs the row's title with the
        # key the entry was filed under -- rather than re-deriving the key with a
        # second copy of gen_key, which is how the two implementations that used
        # to exist here drifted apart.
        resolved_keys = {key for key, _ in new_entries}
        assignments = {entry["title"]: entry["item_name"]
                       for entry in missing_entries
                       if entry["item_name"] in resolved_keys}
        written = set_bib_keys(assignments)
        if written:
            print(f"  Wrote {written} Bib key(s) into {os.path.basename(TABLE_PATH)}")

    # Only write when something actually changed. Completion is recorded in
    # .pipeline_state.json, so there is no longer any reason to rewrite an
    # unchanged file just to advance its mtime.
    if new_bib_text != bib_text:
        with open(BIB_PATH, "w") as f:
            f.write(new_bib_text)
        print(f"  Replaced {n_replaced} entries, appended {n_appended} entries → orig.bib updated")
    else:
        print("  No bib changes — orig.bib left untouched")
    return upgraded, n_appended, n_still_arxiv, not_found


# ── Step 4 ─────────────────────────────────────────────────────────────────────

def step4_build_bib(dry_run: bool):
    if dry_run:
        print("  (dry-run: skipped)")
        return None
    import build_bib
    return build_bib.main()


# ── Step 5 ─────────────────────────────────────────────────────────────────────

def step5_rebuild_tex(dry_run: bool, cats) -> None:
    if dry_run:
        print("  (dry-run: skipped)")
        return
    import rebuild_tex
    rebuild_tex.main(cats)


# ── Step 6 ─────────────────────────────────────────────────────────────────────

_OUTER_FILES = [
    "citations.csv",
    "profile_stats.json",
    "papers.csv",
    "orig.bib",
    "identity.json",
    "resolve_attempts.json",
    ".pipeline_state.json",
    "venues.yaml",
    "WORKLIST.md",
    "overleaf",  # submodule pointer
]
# The .bst files are here because patch_bst_author() edits them: a fork that
# changes AUTHOR_NAME has its name-bolding style rewritten on disk, and leaving
# that out of the push meant the fix never reached the project that compiles.
_OVERLEAF_FILES = ["main.tex", "Wzmn.bib",
                   "planyr-rev.bst", "planyr.bst", "iclr-based.bst"]


def _git_commit_and_push(repo_dir: str, files: list[str], message: str, remote: str) -> bool:
    """Stage files, commit if changed, rebase onto the remote, push.

    Returns True on success. The rebase matters for Overleaf: editing the
    project in Overleaf's own editor advances its remote, after which every
    push from here is rejected until someone pulls by hand. Rebasing first
    makes that self-healing instead of a standing manual chore.
    """
    existing = [f for f in files if os.path.exists(os.path.join(repo_dir, f))]
    add = subprocess.run(["git", "-C", repo_dir, "add", "--"] + existing,
                         capture_output=True, text=True)
    if add.returncode != 0:
        print(f"  [{remote}] git add failed: {add.stderr.strip()}")
        return False

    diff = subprocess.run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode == 0:
        print(f"  [{remote}] Nothing to commit.")
    else:
        result = subprocess.run(
            ["git", "-C", repo_dir, "commit", "-m", message],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  [{remote}] Commit failed: {result.stderr.strip()}")
            return False
        print(f"  [{remote}] Committed.")

    # No prompting, ever. This runs from launchd with no terminal attached, where
    # git's default is to raise a GUI password dialog behind whatever you are
    # doing and wait -- holding the run lock, so the following week's run is
    # skipped too. Failing is recoverable; blocking indefinitely is not.
    git_env = overleaf_auth.noninteractive_env()

    def _push() -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", repo_dir, "push", remote],
                              capture_output=True, text=True, env=git_env)

    print(f"  [{remote}] Pushing…", end=" ", flush=True)
    push = _push()
    if push.returncode == 0:
        print("ok")
        return True

    # Rejected, most likely because the remote moved. Rebase and retry once.
    print("rejected; rebasing onto remote…", end=" ", flush=True)
    pull = subprocess.run(
        ["git", "-C", repo_dir, "pull", "--rebase", "--autostash", remote],
        capture_output=True, text=True, env=git_env,
    )
    if pull.returncode != 0:
        print("FAILED")
        print(f"    pull --rebase failed: {overleaf_auth.redact(pull.stderr.strip()[:400])}")
        subprocess.run(["git", "-C", repo_dir, "rebase", "--abort"], capture_output=True)
        return False

    push = _push()
    if push.returncode == 0:
        print("ok (after rebase)")
        return True
    print("FAILED")
    print(f"    {overleaf_auth.redact(push.stderr.strip()[:400])}")
    return False


def step6_worklist(dry_run: bool) -> None:
    """Regenerate WORKLIST.md so open items outlive the run's stdout."""
    print("\n[Step 6] Regenerating WORKLIST.md")
    if dry_run:
        print("  (dry-run: skipped)")
        return
    result = subprocess.run(
        [sys.executable, os.path.join(FILE_DIR, "scripts", "worklist.py"), "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # A worklist failure must not fail the pipeline: the generated CV is
        # still correct, only the to-do summary is missing.
        print(f"  Warning: could not generate WORKLIST.md: {result.stderr.strip()[:300]}")
        return
    if os.path.exists(WORKLIST_PATH):
        with open(WORKLIST_PATH) as f:
            open_items = sum(1 for line in f if line.startswith("- "))
        print(f"  {open_items} open item(s) → WORKLIST.md")


def step7_push(dry_run: bool) -> bool:
    """Push both repos. Returns True only if every push succeeded."""
    print("\n[Step 7] Committing and pushing to Overleaf + GitHub")
    if dry_run:
        print("  (dry-run: skipped)")
        return True

    message = "chore: auto-update publications pipeline output"
    # Submodule (overleaf/) → Overleaf, then the outer repo → GitHub. Overleaf
    # goes first so the submodule pointer the outer commit records already exists
    # on the remote.
    overleaf_ok = _git_commit_and_push(OVERLEAF_DIR, _OVERLEAF_FILES, message, "origin")
    github_ok = _git_commit_and_push(FILE_DIR, _OUTER_FILES, message, "origin")
    return overleaf_ok and github_ok


# ── Main ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Update the publications pipeline end-to-end",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Steps auto-skip when their inputs' contents are unchanged since the
step last succeeded. Use --force to bypass that.

Companion commands, none needed routinely:
  python scripts/worklist.py            regenerate WORKLIST.md alone (no network)
  python scripts/worklist.py --check    exit 1 if anything needs a decision
  python scripts/dedupe.py --dry-run    find papers listed twice
  python scripts/refresh_venues.py      refresh venue rankings and metrics
  python scripts/install_schedule.py    install the weekly local run
  python init_new_author.py             wipe personal data, for a fork
""",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying files")
    parser.add_argument("--force", action="store_true",
                        help="Run all steps regardless of file mtimes")
    parser.add_argument("--fetch-age", type=float, default=24.0, metavar="HOURS",
                        help="Re-fetch citations.csv if older than this many hours (default: 24)")
    parser.add_argument("--skip-fetch",        action="store_true", help="Skip step 1")
    parser.add_argument("--skip-new",          action="store_true", help="Skip step 2")
    parser.add_argument("--skip-resolve",      action="store_true", help="Skip step 3")
    parser.add_argument("--skip-publications", action="store_true", help="Skip step 4")
    parser.add_argument("--skip-tex",          action="store_true", help="Skip step 5")
    parser.add_argument("--no-push",           action="store_true", help="Skip step 6 (commit + push)")
    parser.add_argument("--no-notify",         action="store_true",
                        help="Do not post a desktop/CI notification on failure")
    parser.add_argument("--user", default=None,
                        help="Google Scholar user ID (passed to fetch_citations.py)")
    args = parser.parse_args(argv)

    problems = preflight()
    if problems:
        print("Cannot start:")
        for problem in problems:
            print(f"  - {problem}")
        # stdout is buffered and stderr is not, so flush before notifying or the
        # notification prints above the explanation of it.
        sys.stdout.flush()
        notify.failure("Publications pipeline cannot start.",
                       "; ".join(p.split(chr(10))[0] for p in problems),
                       enabled=not args.no_notify)
        sys.exit(1)

    push_problem = check_push_credential(not (args.no_push or args.dry_run))
    if push_problem:
        print(f"Warning: {push_problem}\n")
        sys.stdout.flush()

    if not args.skip_resolve:
        print(f"{describe_s2_key()}\n")

    n_added = 0
    n_upgraded = n_appended = n_still_arxiv = 0
    not_found: list = []
    cats = None
    state = PipelineState.load()

    def _should_run(step: str, skip_flag: bool) -> bool:
        """Decide and explain. Returns True when the step should execute."""
        if skip_flag:
            print("  Skipped (--skip flag).")
            return False
        if args.force:
            return True
        changed = state.changed_inputs(step, STEP_INPUTS[step])
        lost = state.changed_outputs(step, STEP_OUTPUTS[step])
        if not changed and not lost:
            print(f"  Auto-skipped — inputs unchanged since "
                  f"{state.steps[step]['completed_at']} "
                  f"({', '.join(os.path.basename(p) for p in STEP_INPUTS[step])}).")
            return False
        if changed:
            print(f"  Inputs changed: {', '.join(os.path.basename(p) for p in changed)}")
        if lost:
            print(f"  Output no longer matches what this step wrote: "
                  f"{', '.join(os.path.basename(p) for p in lost)}")
        return True

    # Step 1 — re-fetch when the recorded fetch has aged out. Time-based rather
    # than content-based: the whole point is to notice that the *remote* changed.
    fetch_age = state.age_hours("fetch")
    print("[Step 1] Fetching Scholar profile")
    if args.skip_fetch:
        print("  Skipped (--skip-fetch).")
    elif not args.force and fetch_age < args.fetch_age:
        print(f"  Auto-skipped — last fetch was {fetch_age:.1f}h ago "
              f"(threshold: {args.fetch_age}h, use --force to override).")
    else:
        step1_fetch(args.dry_run, args.user)
        if not args.dry_run:
            state.mark_done("fetch", [CITATIONS_CSV])
            state.save()

    # Step 2 — cheap and idempotent, so it always runs unless explicitly skipped.
    if args.skip_new:
        print("\n[Step 2] Skipped.")
    else:
        n_added = step2_add_new_papers(args.dry_run)
        step2b_enrich(args.dry_run)

    print("\n[Step 3] Resolving BibTeX entries")
    if _should_run("resolve", args.skip_resolve):
        # Reset here rather than inside the step, so the count belongs to this
        # run's lookups and nothing else.
        reset_unanswered_lookups()
        n_upgraded, n_appended, n_still_arxiv, not_found = step3_resolve(args.dry_run)
        if not args.dry_run:
            # A run during which a source went silent has not finished asking.
            # Marking it done would freeze that answer in until an unrelated
            # input changed, which for a paper published last week means being
            # cited as a preprint for another week.
            silent = unanswered_lookups()
            if silent:
                print(f"  Not recording step 3 as done: {silent} lookup(s) got no "
                      f"answer, so the next run asks again.")
            else:
                state.mark_done("resolve", STEP_INPUTS["resolve"],
                                STEP_OUTPUTS["resolve"])
                state.save()

    print("\n[Step 4] Building the bibliography")
    if _should_run("build_bib", args.skip_publications):
        cats = step4_build_bib(args.dry_run)
        if not args.dry_run:
            state.mark_done("build_bib", STEP_INPUTS["build_bib"],
                            STEP_OUTPUTS["build_bib"])
            state.save()

    print("\n[Step 5] Rebuilding overleaf/main.tex")
    if _should_run("rebuild_tex", args.skip_tex):
        step5_rebuild_tex(args.dry_run, cats)
        if not args.dry_run:
            state.mark_done("rebuild_tex", STEP_INPUTS["rebuild_tex"],
                            STEP_OUTPUTS["rebuild_tex"])
            state.save()

    step6_worklist(args.dry_run)

    # Step 7 — commit + push to origin and overleaf
    push_ok = True
    if args.no_push:
        print("\n[Step 7] Skipped (--no-push).")
        print("  Overleaf still serves its previous version. Compiling there now "
              "would show stale numbers; re-run without --no-push to publish.")
    else:
        push_ok = step7_push(args.dry_run)

    print("\n" + "═" * 52)
    print(f"  Step 2: {n_added} new paper(s) added to the publications table")
    print(f"  Step 3: {n_upgraded} arXiv → published  |  "
          f"{n_appended} new entries appended  |  "
          f"{n_still_arxiv} still arXiv")
    if not_found:
        print(f"\n  {len(not_found)} paper(s) need a manual bib lookup "
              f"(also listed in WORKLIST.md):")
        for title, key in not_found:
            print(f"    [{key}] {title}")
    print("═" * 52)

    # A silent failure in an unattended run is the failure mode that matters:
    # the CV keeps looking current while going stale. Exit non-zero so launchd,
    # cron or Actions notices, and notify on the way out.
    if not push_ok:
        notify.failure(
            "Publications pipeline could not push.",
            "Generated files are correct on disk but GitHub and/or Overleaf are "
            "not updated. Re-run `python update.py --skip-fetch` after fixing the "
            "remote.",
            enabled=not args.no_notify,
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        # One run at a time: the weekly scheduled run and a manual one would
        # otherwise interleave their reads and writes and lose an update.
        with RunLock():
            main()
    except AlreadyRunning as exc:
        print(f"Not starting: {exc}")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        notify.failure(f"{type(exc).__name__}: {exc}")
        raise
