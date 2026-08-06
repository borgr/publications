# Publications Pipeline

Keeps a publications CV on Overleaf in sync with Google Scholar, from one
command. Built to be re-run for years with as little manual work as possible:
every step is idempotent, anything it cannot decide lands in
[WORKLIST.md](WORKLIST.md), and a failure is never silent.

```
Google Scholar ──► citations.csv ──┐
                   profile_stats.json
                                   ├──► orig.bib ──► overleaf/Wzmn.bib ──► overleaf/main.tex
papers.csv ────────────────────────┤     (resolved)   (+ venue info,        (+ \nocite blocks,
  (venue, tags, authorship)        │                   citation counts)      citations, h-index)
venues.yaml ───────────────────────┘
identity.json  (harvested IDs)
```

## The one command

```bash
python update.py
```

Everything else is occasional. `update.py` exits non-zero on failure and posts a
desktop notification, so an unattended run cannot fail quietly while the CV keeps
looking current.

```bash
python update.py --dry-run    # show what would change: no writes, no network
python update.py --force      # ignore the "inputs unchanged" checks
python update.py --no-push    # build locally, leave the remotes alone
python update.py --skip-fetch # reuse the citation counts already on disk
```

### What it does

Each step is skipped when the **contents** of its inputs are unchanged since it
last succeeded (recorded in `.pipeline_state.json`) — content hashes, not mtimes,
so a fresh clone behaves correctly and a no-op rewrite does not cascade.

| # | Step | What it does |
|---|------|--------------|
| 1 | fetch | Scrapes the Scholar profile → `citations.csv`, `profile_stats.json`. Time-based (`--fetch-age`, default 24h). |
| 2 | new papers | Adds anything in Scholar that is not in `papers.csv` yet. |
| 2b | enrich | Fills blank Authors from Scholar, and resolves a venue from the paper's BibTeX entry. This is what moves a paper out of the CV's ArXiv section once it is published. |
| 3 | resolve | Upgrades arXiv entries in `orig.bib` to their published version, and looks up rows that have no entry. |
| 4 | build | `orig.bib` + `papers.csv` + `venues.yaml` → `overleaf/Wzmn.bib`. |
| 5 | tex | Updates `\nocite{}` blocks, total citations and h-index in `overleaf/main.tex`. |
| 6 | worklist | Regenerates `WORKLIST.md`. |
| 7 | push | Commits and pushes to GitHub and Overleaf, rebasing first if a remote moved. |

## Occasionally

```bash
python scripts/worklist.py          # regenerate WORKLIST.md alone (no network)
python scripts/worklist.py --check  # exit 1 if anything needs a decision
python scripts/dedupe.py --dry-run  # find papers listed twice, keep the published one
python scripts/refresh_venues.py    # refresh venue rankings and impact metrics
python -m pytest tests/ -q          # the test suite
```

## Once, when setting up

```bash
python scripts/install_schedule.py       # weekly local run (macOS launchd)
python scripts/install_schedule.py --show  # print the plist / cron line instead
python scripts/migrate_to_csv.py         # only if you still have the .xlsx
python init_new_author.py                # wipe personal data, for a fork
```

The step scripts (`fetch_citations.py`, `build_bib.py`, `rebuild_tex.py`,
`resolve_arxiv.py`) also run standalone, which is useful when debugging one stage.

## Setting it up

```bash
git clone --recurse-submodules https://github.com/borgr/publications.git
cd publications
pip install -r requirements.txt
```

Then edit `config.py` — that is the only file that is author-specific:

```python
AUTHOR_NAME = "Your Name"
SCHOLAR_USER_ID = "..."      # from your Scholar profile URL
S2_API_KEY = ""              # optional, see below
CONTACT_EMAIL = ""           # optional, for OpenAlex's polite pool
```

`curl` and `git` must be on your PATH.

### A Semantic Scholar API key is worth the two minutes

Semantic Scholar's unauthenticated access is *"1000 requests per second shared
among all unauthenticated users"* — a global pool, so a long run gets throttled
almost immediately. A free key gets *"1 RPS"* reserved, which is slower on paper
but actually completes. It matters more than it looks: the ACL Anthology and
OpenReview are both reached *through* Semantic Scholar, so losing it loses three
sources.

Request one at <https://www.semanticscholar.org/product/api>, then set
`S2_API_KEY` in `config.py` or in the environment. Without a key everything still
works, with more waiting.

## Scheduling

Split deliberately, because Scholar blocks datacenter IP ranges — a hosted runner
gets a CAPTCHA, not data:

- **Locally**, weekly, via `scripts/install_schedule.py`. This is the one that
  fetches from Scholar.
- **[GitHub Actions](.github/workflows/ci.yml)** covers what does not need
  Scholar: the test suite on two Python versions, a rebuild from committed data,
  a determinism check, and a failure if the table has duplicate rows or an
  ambiguous citation join. No secrets, works in a fork.

### Letting CI see your Overleaf project (optional)

Without this, nothing outside your own machine can tell whether the CV Overleaf
compiles matches the committed data — the failure that motivated it was a run of
`--no-push` runs that left Overleaf hundreds of citations behind while every run
reported success.

1. In Overleaf: **Menu → Git**, and copy the URL with its token.
2. In GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**, named `OVERLEAF_GIT_URL`.

CI then rebuilds the CV from the committed data and **fails if Overleaf would
compile something different**. To have CI push the fix rather than just report
it, add a repository *variable* `PUBLISH_TO_OVERLEAF` set to `true`. That is off
by default: Overleaf is a document you also edit by hand, so writing to it is a
decision, not a default. It rebases before retrying, so a push and a hand edit
racing does not fail the run.

With no secret set, the job prints how to enable itself and passes — a fork is
never red for a project it does not have. The same staleness is reported locally
in `WORKLIST.md`, which needs no credentials.

## Files

### Data you edit

| File | Purpose |
|------|---------|
| `papers.csv` | **Source of truth.** One row per paper: venue, authors, year, BibTeX key, and the tag flags that drive CV sections. Opens in Excel/Numbers/LibreOffice. |
| `venues.yaml` | Venue rankings, impact metrics, and the sentence each venue prints. |
| `config.py` | Author name, Scholar ID, optional API keys. |
| `orig.bib` | Curated BibTeX. Mostly maintained by step 3; hand-edit what it cannot resolve. |

### Data the pipeline owns

Generated but **committed**, because each is expensive to rebuild or useful to
browse: `citations.csv`, `profile_stats.json`, `identity.json` (harvested
identifiers), `resolve_attempts.json` (retry counters), `.pipeline_state.json`,
`WORKLIST.md`, `overleaf/Wzmn.bib`.

`enhanced.bib` is **legacy — do not read.** It is an old snapshot of
`build_bib.py`'s output from when it wrote there instead of
`overleaf/Wzmn.bib`. Nothing regenerates it, so it drifts further behind every
run. To use the bibliography from outside this repo, read `orig.bib`.

### Code

| File | Purpose |
|------|---------|
| `update.py` | The orchestrator. |
| `fetch_citations.py` | Scholar scraper, including each paper's stable `citation_for_view` ID. |
| `table_io.py` | Reads/validates/writes `papers.csv`, by column name. |
| `identity.py` | Stable identifiers, and the joins built on them. |
| `citations_io.py` | `citations.csv` reading and writing. |
| `resolve_arxiv.py` | arXiv → published BibTeX, via DBLP / S2 / ACL / OpenReview / DOI / OpenAlex. |
| `build_bib.py` | Builds `Wzmn.bib` and assigns each paper to a CV section. |
| `rebuild_tex.py` | Updates `main.tex` in place. |
| `bib_utils.py` | Brace-counting BibTeX parser, text normalization, publication ranking. |
| `venues.py` | Loads `venues.yaml`. |
| `pipeline_state.py` | Content-hash step skipping. |
| `notify.py` | Failure notification (macOS Notification Center, Actions annotation). |
| `papers_fig.py`, `papers_graph.py` | Standalone figures. Not part of the pipeline; needs `requirements-figures.txt`. |

## How papers are matched across sources

Titles are not stable: BibTeX braces capitalization (`{B}aby{LM}`), Scholar
lowercases subtitles, papers get retitled between preprint and publication, and
the table is typed by hand. So matching is by **identifier**, and titles are only
the bootstrap:

1. **Stable ID.** Scholar's `citation_for_view` per paper; `externalIds` from
   Semantic Scholar for ArXiv/DOI/ACL/DBLP together — the crosswalk for when the
   ACL record knows no arXiv ID and vice versa. Recorded in `identity.json` the
   first time it is seen, so a fuzzy match happens at most once per paper.
2. **Normalized title.** Case, punctuation and BibTeX braces stripped. An
   identity claim, not a guess.
3. **Fuzzy title**, only with a clear margin over the runner-up, and reported.

Anything ambiguous is reported, never guessed. Step 2's "is this paper already
known?" calls the same matcher as the citation join, deliberately: when they
disagreed, step 2 appended a duplicate row for every fuzzy-matched paper on
every run.

Two Scholar records for one paper are **summed** — Scholar splits a paper across
records until you merge the versions, and each record counts different citing
papers. Records that are *not* the same paper are never summed.

## Which entry wins when a paper appears twice

`bib_utils.publication_rank` and `choose_published` are the single rule, used by
step 3 (never downgrade), by the build (emit the version of record), and by
`scripts/dedupe.py`:

1. published beats preprint — the version of record is what a CV should cite;
2. within the same class, the newer year wins, because two preprints of one paper
   are its v1 and v2 and the newer carries the current title;
3. then publication rank, content length and key, so the result is stable.

Duplicates are found by **identifier** as well as by title, which catches the
retitled ones no title comparison can.

## Venues

`venues.yaml` holds each venue's `kind` (`journal`, `conference`, or `other` for a
real outlet with no ranking, like a blog), its `description` (the sentence the CV
prints), and `match:` phrases used to recognise it in a raw venue string.

`scripts/refresh_venues.py` refreshes the numbers from Google Scholar Metrics and
OpenAlex. A venue marked `manual: true` keeps its prose; only its `metrics` block
is updated. When this file was created, four of eleven rankings were wrong — EMNLP
and NAACL both claimed to be 1st.

A venue the pipeline cannot place gets no `venueinf` line and its paper is filed
under ArXiv Articles, so unplaceable venues are reported in `WORKLIST.md`. Add a
`match:` phrase to fix one.

## Optional: clibib for the tail

[clibib](https://github.com/delip/clibib) resolves an identifier to BibTeX via a
Zotero translation server. `pip install clibib` and step 3 will use it for
**DOIs**, covering journals and book chapters that DBLP and the ACL Anthology do
not index. It resolved 10 entries on a real run. Without it the pipeline behaves
exactly as before.

Deliberately never used for title search: measured against this repo's own
unresolved papers, its free-text lookup returned a confidently wrong paper 2 times
in 5 with no error raised. Its identifier paths are exact and fast, and its own
README recommends preferring them.

As a manual helper it is genuinely useful for worklist items where you have an
identifier:

```bash
clibib 10.1038/s41586-021-03215-w
```

## Citation counts in the CV

Each entry in `Wzmn.bib` carries a `citations={N}` field, which the BST emits as
`\bibcitecount{N}`. Toggle display in `main.tex`:

```latex
%\newcommand{\bibcitecount}[1]{ \textit{\small[#1 cited]}}  % show
\newcommand{\bibcitecount}[1]{}                              % hide (default)
```

## GitHub ↔ Overleaf

`overleaf/` is a git submodule pointing at the Overleaf project. Step 7 pushes
`main.tex` and `Wzmn.bib` there, then pushes this repo to GitHub. If the Overleaf
remote has moved — because you edited the project in Overleaf's own editor — the
push rebases onto it and retries, so that no longer needs a manual pull.

Pull Overleaf-side edits back with `git -C overleaf pull origin`.
