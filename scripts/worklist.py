#!/usr/bin/env python3
"""Generate WORKLIST.md: everything the pipeline cannot decide on its own.

Run standalone -- it re-reads the data files, so the worklist can be regenerated
at any time. The only network it uses is one `git fetch` of the Overleaf remote,
to tell output that has not been published from output that CI published while
this clone was not looking; that fetch is optional and failing it changes
nothing but the freshness of that one section:

    python scripts/worklist.py
    python scripts/worklist.py --check    # exit 1 if anything is open (for CI)

Why this exists: the pipeline used to print its open questions to stdout and
then exit, so anything not read in the moment was lost. Unresolved bib lookups,
fuzzy citation matches and unknown venues all silently accumulated. Writing them
to a committed file means they are browsable on GitHub and survive the run.

Only open items appear. A section that is absent is done.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_bib
from bib_edit import get_missing_bib_entries
from bib_utils import find_duplicate_keys, parse_bibtex, read_df
from citations_io import read_citation_rows
from identity import (
    MATCH_EXACT_ID,
    IdentityStore,
    find_duplicate_titles,
    join_citations,
)
from resolve_arxiv import load_attempts
from table_io import rows_named
from venues import Venues

WORKLIST_PATH = os.path.join(ROOT, "WORKLIST.md")
BIB_PATH = os.path.join(ROOT, "orig.bib")
CITATIONS_CSV = os.path.join(ROOT, "citations.csv")


# How an item behaves over time. Printed on every section, because "will this
# keep coming back?" decides whether it is worth automating or worth doing once.
ONE_OFF = ("one-off",
           "Fix once and it is gone; nothing regenerates it.")
SELF_RESOLVING = ("resolves itself",
                  "The next run clears this without you doing anything.")
RECURRING = ("recurring",
             "Will reappear as new papers arrive. Automating it is worthwhile.")
EXTERNAL = ("waiting on a source",
            "Cannot be fixed here -- it depends on an external record.")
INFORMATIONAL = ("informational",
                 "Handled automatically; listed so the decision is visible.")


class Section:
    def __init__(self, title, blurb, lines, nature=ONE_OFF):
        self.title = title
        self.blurb = blurb
        self.lines = lines
        self.nature = nature


OVERLEAF_DIR = os.path.join(ROOT, "overleaf")
# The files whose staleness is visible in the compiled CV.
_PUBLISHED_FILES = ("main.tex", "Wzmn.bib")


def _git(repo, *args):
    """Run git, returning stdout or None if it could not run."""
    import subprocess
    try:
        result = subprocess.run(["git", "-C", repo, *args],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _differs_from_remote():
    """Which published files the Overleaf remote does not already have verbatim.

    Returns every published file when there is nothing to compare against -- no
    upstream, or git could not run -- so an unknown remote degrades to reporting
    the local state rather than to reporting nothing at all.
    """
    diff = _git(OVERLEAF_DIR, "diff", "--name-only", "@{upstream}",
                "--", *_PUBLISHED_FILES)
    if diff is None:
        return set(_PUBLISHED_FILES)
    # An untracked file is on no remote by definition, and `git diff` cannot see
    # one: the state of a fresh fork whose project has no Wzmn.bib yet.
    untracked = _git(OVERLEAF_DIR, "ls-files", "--others", "--exclude-standard",
                     "--", *_PUBLISHED_FILES) or ""
    return set(diff.split()) | set(untracked.split())


def _refresh_remote_ref():
    """Update what we know of the Overleaf remote, so the comparison below is
    against what it serves now and not against the last time anyone pushed.

    Best-effort on purpose. No credential, no network, no submodule and no
    upstream all mean "compare against what is already known" rather than
    "fail" -- the worklist has to be regeneratable offline. GIT_TERMINAL_PROMPT=0
    so a missing credential cannot turn this into a password prompt nobody is
    there to answer.
    """
    import subprocess
    try:
        subprocess.run(["git", "-C", OVERLEAF_DIR, "fetch", "--quiet", "origin"],
                       capture_output=True, timeout=30,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        pass


def _unpublished_output():
    """Report generated output that exists locally but has not reached Overleaf.

    The failure this catches is silent by construction and cost a real
    compile-and-wonder-why: the pipeline had been run many times with --no-push,
    so main.tex and Wzmn.bib were correct on disk while Overleaf still served the
    previous version. Nothing anywhere said so -- the run reported success,
    because generating the files *was* the success.

    What matters is whether Overleaf serves these bytes, which is not the same
    question as whether this checkout has committed them. CI can publish too, and
    then the answers diverge: the local clone falls a commit behind with files
    that are byte-identical to what Overleaf already compiles. Asked about the
    local HEAD, that reads as two unpublished files, on every run, and no amount
    of pushing clears it -- a section that cries wolf until it gets ignored.
    """
    out = []
    if not os.path.isdir(OVERLEAF_DIR):
        return out
    _refresh_remote_ref()

    dirty = _git(OVERLEAF_DIR, "status", "--porcelain", "--", *_PUBLISHED_FILES)
    # Porcelain lines are `XY <path>`, where X or Y may be a space. Splitting the
    # *unstripped* line matters: stripping first eats the leading status column
    # and shifts the path by one, which produced "overleaf/ain.tex".
    changed = [line[3:].split(" -> ")[-1].strip()
               for line in (dirty or "").splitlines() if line.strip()]
    changed = [p for p in changed if p in _differs_from_remote()]
    if changed:
        out.append((
            f"CV output built but not committed ({len(changed)})",
            "These are generated and correct on disk, but Overleaf still serves "
            "the previous version — compiling there will show stale numbers. "
            "`python update.py` (without --no-push) commits and pushes them.",
            [f"- `overleaf/{path}` has uncommitted changes" for path in changed]))

    # Commits made locally that the Overleaf remote has not got.
    ahead = _git(OVERLEAF_DIR, "log", "--oneline", "@{upstream}..HEAD")
    if ahead and ahead.strip():
        out.append((
            f"CV output committed but not pushed ({len(ahead.strip().splitlines())})",
            "Committed in the overleaf/ submodule but not on the Overleaf remote, "
            "so the project there is behind. `git -C overleaf push origin` sends "
            "them, or re-run `python update.py`.",
            [f"- {line}" for line in ahead.strip().splitlines()]))
    return out


def gather():
    """Collect open items. Returns (sections, total_count)."""
    with open(BIB_PATH) as f:
        bib_text = f.read()
    parsed = parse_bibtex(bib_text)
    df = read_df()
    names = list(df["Name"].dropna())
    citations = read_citation_rows(CITATIONS_CSV)
    store = IdentityStore.load()
    venues = Venues.load()
    attempts = load_attempts()

    sections = []

    # ── duplicates: these break the citation join by construction ────────────
    dup_rows = find_duplicate_titles(names)
    if dup_rows:
        lines = []
        for group in dup_rows.values():
            # One (name, key) per row. Looking each name up separately reads the
            # same row twice when both rows carry the identical title, and this
            # list is what the author reads to decide which key to delete -- it
            # named the surviving key twice and never mentioned the other.
            rows = rows_named(df, group)
            keys = ", ".join(f"`{key or '?'}`" for _name, key in rows)
            lines.append(f"- Same paper on {len(rows)} rows ({keys}):")
            for name, key in rows:
                lines.append(f"  - {name} — `{key or '?'}`")
        sections.append(Section(
            f"Duplicate rows in the publications table ({len(dup_rows)})",
            "Two rows for one paper means both compete for the same citation "
            "count, and both are emitted into the CV. Delete the row whose "
            "BibTeX key you do not want to keep.",
            lines, nature=ONE_OFF))

    dup_keys = find_duplicate_keys(parsed)
    if dup_keys:
        sections.append(Section(
            f"Duplicate BibTeX keys in orig.bib ({len(dup_keys)})",
            "Lookups resolve to whichever entry parsed first.",
            [f"- `{k}` appears {n} times" for k, n in sorted(dup_keys.items())]))

    # ── papers with no bibliography entry ────────────────────────────────────
    missing = get_missing_bib_entries(bib_text, df=df)
    if missing:
        lines = []
        for entry in sorted(missing, key=lambda e: -attempts.get(e["item_name"], 0)):
            tried = attempts.get(entry["item_name"], 0)
            suffix = f" — {tried} failed lookup(s) so far" if tried else ""
            lines.append(f"- `{entry['item_name']}` — {entry['title']}{suffix}")
        sections.append(Section(
            f"Papers with no BibTeX entry ({len(missing)})",
            "Retried automatically every run against DBLP, Semantic Scholar, "
            "the ACL Anthology, OpenReview and OpenAlex. An entry with several "
            "failed lookups is one no source indexes yet -- usually a very recent "
            "preprint, a blog post or a workshop paper. Those need either time "
            "or a hand-pasted entry; `clibib <doi-or-url>` helps when you have "
            "an identifier. The count is lookups that completed and found "
            "nothing: a lookup a source never answered is not counted, so the "
            "number means \"no source has it\" rather than \"the network was "
            "flaky\".",
            lines, nature=EXTERNAL))

    # ── citation matches that a human should confirm once ────────────────────
    result = join_citations(citations, names, store=store)
    if result.needs_review:
        lines = []
        for name, incoming, tier, score in sorted(result.needs_review,
                                                  key=lambda r: r[3]):
            lines.append(f"- {score:.0%} ({tier}) — table: {name}")
            lines.append(f"  - Scholar: {incoming}")
        sections.append(Section(
            f"Citation counts matched by title, not by identifier "
            f"({len(result.needs_review)})",
            "Matched and in use, but on title similarity rather than a stable "
            "identifier -- the paper was retitled between preprint and "
            "publication. Confirm each is really the same paper. The next "
            "`fetch_citations.py` run records its Scholar ID, after which it "
            "matches exactly and drops off this list permanently.",
            lines, nature=SELF_RESOLVING))

    if result.ambiguous:
        lines = []
        for name, candidates, total in result.ambiguous:
            lines.append(f"- table: {name} (using {total})")
            for title, tier, score in candidates:
                lines.append(f"  - {score:.0%} ({tier}) {title}")
        sections.append(Section(
            f"Ambiguous citation matches ({len(result.ambiguous)})",
            "Scholar records that are NOT the same paper landed on one table "
            "row. Their counts are deliberately not added together; the "
            "most-trusted record is used. Usually means a row title is wrong or "
            "too generic.",
            lines, nature=ONE_OFF))

    if result.unmatched:
        lines = [f"- {value if value is not None else '—'} citations — {title}"
                 for title, value in sorted(result.unmatched,
                                            key=lambda r: -(r[1] or 0))]
        sections.append(Section(
            f"Scholar records with no row in the table ({len(result.unmatched)})",
            "A Scholar record that no row claims. Step 2 adds genuinely new "
            "papers by itself, so a record that persists here is one of three "
            "things, and each is a decision only you can make: a paper you have "
            "chosen not to list; a misattribution to remove from your Scholar "
            "profile; or a table row that was deleted while `identity.json` "
            "still binds its Scholar ID -- which step 2 will *not* re-add, "
            "because the ID is already claimed and the paper therefore does not "
            "look new. For that last one, delete the record for that key from "
            "`identity.json` and the next run puts the row back.",
            lines, nature=ONE_OFF))

    # ── venues that fall through to the draft section ────────────────────────
    # Two different problems, which want different fixes: a venue genuinely
    # absent from venues.yaml, versus a Venue cell still holding the raw string
    # Scholar reported. Step 2 writes Scholar's venue text verbatim when it adds
    # a row, and that text never matches a key, so every auto-added paper needs
    # its Venue cell tidied once.
    def _looks_unparsed(key):
        return (len(key) > 15 or "," in key or "…" in key
                or any(ch.isdigit() for ch in key))

    unknown, unparsed = {}, {}
    for _, row in df.iterrows():
        raw = row.get("Venue")
        if not isinstance(raw, str) or not raw.strip():
            continue
        low = raw.lower()
        if "xiv" in low or "review" in low or "patent" in low:
            continue
        # Workshop papers are categorised by their flag, not their venue, and
        # deliberately carry no venueinf line.
        if row.get("Workshop-paper") == 1:
            continue
        # Not a paper (a proceedings volume, a patent): its venue is irrelevant.
        if row.get("Paper") == 0:
            continue
        key = build_bib.simplify_venue(raw)
        if not key or venues.known(key):
            continue
        bucket = unparsed if _looks_unparsed(key) else unknown
        bucket.setdefault(key, set()).add((raw.strip(), str(row.get("Name") or "")))

    if unparsed:
        lines = []
        for _key, entries in sorted(unparsed.items()):
            raw, name = sorted(entries)[0]
            lines.append(f"- `{raw[:80]}`")
            lines.append(f"  - on: {name[:90]}")
        sections.append(Section(
            f"Venue cells still holding a raw Scholar string ({len(unparsed)})",
            "Step 2 copies Scholar's venue text verbatim, and this text does "
            "not reduce to a venue key -- so the paper gets no `venueinf` line "
            "and is filed under ArXiv Articles. Step 2b now rewrites these "
            "automatically when Scholar's own venue string can be resolved; "
            "what is left needs a `match:` phrase in venues.yaml, or is not a "
            "venue at all (a blog post, a patent).",
            lines, nature=RECURRING))

    if unknown:
        lines = []
        for key, entries in sorted(unknown.items()):
            examples = "; ".join(sorted({raw for raw, _ in entries}))
            lines.append(f"- `{key}` — from: {examples[:130]}")
        sections.append(Section(
            f"Venues missing from venues.yaml ({len(unknown)})",
            "A venue with no entry gets no `venueinf` line and its papers are "
            "filed under ArXiv Articles. Add it to `venues.yaml` with a `kind` "
            "of journal or conference, then run "
            "`python scripts/refresh_venues.py` to fill in the ranking.",
            lines, nature=RECURRING))

    if result.too_close:
        sections.append(Section(
            f"Scholar records matching two rows equally well ({len(result.too_close)})",
            "Not attributed to either row, because picking one would be a coin "
            "flip. Make the two row titles distinguishable.",
            [f"- {score:.0%} — {title} ({value if value is not None else '—'} citations)"
             for title, value, score in result.too_close], nature=ONE_OFF))

    if result.aggregated:
        sections.append(Section(
            f"Citation counts summed across Scholar records ({len(result.aggregated)})",
            "Scholar holds the preprint and the published version as separate "
            "records; their counts are added, which is what Scholar itself shows "
            "once the versions are merged. Nothing to do -- merging them in your "
            "Scholar profile just makes it exact at the source.",
            [line for name, cands, total in result.aggregated
             for line in [f"- {total} total — {name}"]
                        + [f"  - {score:.0%} ({tier}) {title}"
                           for title, tier, score in cands]],
            nature=INFORMATIONAL))

    # Venues placed by the truncation fallback rather than an explicit match.
    truncated = {}
    for _, row in df.iterrows():
        raw = row.get("Venue")
        if not isinstance(raw, str) or not raw.strip():
            continue
        low = raw.lower()
        if "xiv" in low or "review" in low or "patent" in low:
            continue
        key, how = build_bib.venue_resolution(raw)
        if how != "truncated" or not key or not venues.known(key):
            continue
        # Only mid-word cuts are a risk. "ACL 2024" splits at a space-preceded
        # digit and is safe; "Nature-inspired Computing" splits inside a word and
        # silently becomes `nature`.
        # Risky only when the cut is followed by more *word*: a digit after the
        # key is a year ("ACL2022"), which is unambiguous, whereas a letter means
        # the truncation split a real word ("Nature-inspired" -> `nature`).
        rest = low[len(key):]
        if re.match(r'-?[a-z]', rest):
            truncated.setdefault((raw.strip(), key), 0)
            truncated[(raw.strip(), key)] += 1
    if truncated:
        sections.append(Section(
            f"Venues placed by cutting a word in half ({len(truncated)})",
            "The venue key came from truncating the string mid-word, which "
            "happens to be right here but is right by luck -- the same rule turns "
            "\"Nature-inspired Computing\" into `nature`. Add a `match:` phrase "
            "in venues.yaml to make each one certain.",
            [f"- `{key}` <- {raw[:80]}" for (raw, key), _ in sorted(truncated.items())],
            nature=INFORMATIONAL))

    # Only report a shared identifier when both keys are actually in use. A key
    # with no table row is not emitted into the CV, so it is a stale record in
    # identity.json rather than something to act on -- reporting those made the
    # section stay red after dedupe.py had already fixed the real cases.
    keys_in_use = {str(v).strip() for v in df["Bib"].dropna()
                   if str(v).strip().lower() not in ("", "nan", "none")}
    shared = [(field, value, [k for k in keys if k in keys_in_use])
              for field, value, keys in store.shared_identifiers()]
    shared = [(f, v, k) for f, v, k in shared if len(k) > 1]
    if shared:
        sections.append(Section(
            f"One identifier claimed by two papers in the CV ({len(shared)})",
            "Two rows carry the same Scholar ID or DOI, so the same paper is "
            "listed twice -- and because the titles differ, no title comparison "
            "catches it. `python scripts/dedupe.py` resolves these, keeping the "
            "published version.",
            [f"- {field} `{value}` on: {', '.join(keys)}"
             for field, value, keys in shared], nature=ONE_OFF))

    # ── built but not published ──────────────────────────────────────────────
    for title, blurb, lines in _unpublished_output():
        sections.append(Section(title, blurb, lines, nature=RECURRING))

    # ── identifier conflicts ─────────────────────────────────────────────────
    conflicts = store.conflicts()
    if conflicts:
        sections.append(Section(
            f"Conflicting identifiers ({len(conflicts)})",
            "Two sources reported different values for the same field, which "
            "usually means two papers were conflated. Resolve by deleting the "
            "wrong record from `identity.json`.",
            [f"- `{key}` {field}: {', '.join(str(v) for v in values)}"
             for key, field, values in conflicts], nature=ONE_OFF))

    total = sum(len(s.lines) for s in sections)
    return sections, total, result


def render(sections, result):
    out = ["# What still needs you", "",
           "Regenerated by `python update.py` (or `python scripts/worklist.py`).",
           "**Open items only** — a section that is not here is done.", ""]

    exact = sum(1 for t in result.method.values() if t == MATCH_EXACT_ID)
    out += [f"Citation join: {len(result.matched)} papers matched, "
            f"{exact} by stable Scholar ID.", ""]

    if not sections:
        out += ["Nothing open. ✓", ""]
        return "\n".join(out)

    needs_you = [s for s in sections
                 if s.nature in (ONE_OFF, RECURRING)]
    out += [f"**{len(needs_you)} of {len(sections)} sections need a decision from "
            f"you**; the rest resolve themselves, wait on an external source, or "
            f"are handled automatically and listed for visibility.", ""]

    for section in sections:
        label, explain = section.nature
        out += [f"## {section.title}", "",
                f"*{label}* — {explain}", "",
                section.blurb, ""]
        out += section.lines
        out += [""]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate WORKLIST.md")
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if any item is open (does not write)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    sections, total, result = gather()

    if args.check:
        # Only sections that need a decision count as a failure. Informational
        # and self-resolving ones would make --check permanently red and
        # therefore ignored.
        actionable = [s for s in sections if s.nature in (ONE_OFF, RECURRING)]
        items = sum(len(s.lines) for s in actionable)
        print(f"{items} actionable item(s) in {len(actionable)} section(s) "
              f"({total} total across {len(sections)})")
        for s in actionable:
            print(f"  [{s.nature[0]}] {s.title}")
        return 1 if items else 0

    text = render(sections, result)
    previous = ""
    if os.path.exists(WORKLIST_PATH):
        with open(WORKLIST_PATH) as f:
            previous = f.read()
    if text != previous:
        with open(WORKLIST_PATH, "w") as f:
            f.write(text)
    if not args.quiet:
        print(f"  {total} open item(s) in {len(sections)} section(s) → WORKLIST.md")
        for section in sections:
            print(f"    [{section.nature[0]:<20}] {section.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
