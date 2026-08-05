# Publications Pipeline

Keeps a publications CV on Overleaf in sync with Google Scholar, from one
command. Designed to be re-run for years with as little manual work as possible:
every step is idempotent, anything the pipeline cannot decide itself lands in
[WORKLIST.md](WORKLIST.md), and a failure is never silent.

```
Google Scholar ──► citations.csv ──┐
                   profile_stats.json
                                   ├──► orig.bib ──► overleaf/Wzmn.bib ──► overleaf/main.tex
papers.csv ────────────────────────┤     (resolved)   (+ venue info,        (+ \nocite blocks,
  (venue, tags, authorship)        │                   citation counts)      citations, h-index)
venues.yaml ───────────────────────┘
  (rankings, impact metrics)
```

## Usage

```bash
python update.py                 # everything, end to end
python update.py --dry-run       # show what would change; no writes, no network
python update.py --force         # ignore the "inputs unchanged" checks
python update.py --no-push       # build locally, do not touch the remotes
python update.py --skip-fetch    # reuse the citation counts already on disk

python scripts/worklist.py       # regenerate WORKLIST.md on its own
python scripts/refresh_venues.py # refresh venue rankings and impact metrics
python -m pytest tests/ -q       # the test suite
```

`update.py` exits non-zero if anything failed, and posts a desktop notification
(`--no-notify` to suppress), so an unattended run cannot fail quietly while the
CV keeps looking current.

### Steps

Each step is skipped when the **contents** of its inputs are unchanged since it
last succeeded, recorded in `.pipeline_state.json`. Content hashing rather than
mtimes, so a fresh clone behaves correctly and a no-op rewrite does not cascade.

| # | Step | What it does |
|---|------|--------------|
| 1 | fetch | Scrapes the Scholar profile → `citations.csv`, `profile_stats.json`. Time-based (`--fetch-age`, default 24h). |
| 2 | new papers | Adds anything in Scholar but not in `papers.csv`. |
| 3 | resolve | Upgrades arXiv entries in `orig.bib` to their published version, and looks up rows with no entry. |
| 4 | build | `orig.bib` + `papers.csv` + `venues.yaml` → `overleaf/Wzmn.bib`. |
| 5 | tex | Updates `\nocite{}` blocks, total citations and h-index in `overleaf/main.tex`. |
| 6 | worklist | Regenerates `WORKLIST.md`. |
| 7 | push | Commits and pushes to GitHub and Overleaf, rebasing first if a remote moved. |

## Scheduling

Split deliberately, because Scholar blocks datacenter IP ranges — a hosted
runner gets a CAPTCHA, not data:

```bash
python scripts/install_schedule.py          # weekly local run (macOS launchd)
python scripts/install_schedule.py --show   # print the plist / cron line instead
python scripts/install_schedule.py --uninstall
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) covers what does not need
Scholar: the test suite on two Python versions, a rebuild from committed data, a
determinism check, and a failure if the table has duplicate rows or an ambiguous
citation join. It needs no secrets and works in a fork.

## Files

### Data you edit

| File | Purpose |
|------|---------|
| `papers.csv` | **Source of truth.** One row per paper: venue, authors, year, BibTeX key, and the tag flags that drive CV sections. Opens in Excel/Numbers/LibreOffice. |
| `venues.yaml` | Venue rankings, impact metrics and the sentence each venue prints. `manual: true` protects prose from the refresher. |
| `config.py` | Author name and Scholar user ID. The only file to change when forking. |
| `orig.bib` | Curated BibTeX. Mostly maintained by step 3; hand-edit for anything it cannot resolve. |

### Data the pipeline owns

Generated but **committed**, because each one is expensive to rebuild or useful
to browse: `citations.csv`, `profile_stats.json`, `identity.json` (harvested
identifiers), `resolve_attempts.json` (retry counters), `.pipeline_state.json`,
`WORKLIST.md`, `overleaf/Wzmn.bib`.

### Code

| File | Purpose |
|------|---------|
| `update.py` | The 7-step orchestrator. |
| `fetch_citations.py` | Scholar scraper, including each paper's stable `citation_for_view` ID. |
| `table_io.py` | Reads/validates/writes `papers.csv`; addresses columns by name. |
| `identity.py` | Stable identifiers, and the citation join built on them. |
| `citations_io.py` | `citations.csv` reading and writing. |
| `resolve_arxiv.py` | arXiv → published BibTeX, via DBLP / S2 / ACL / OpenReview / DOI. |
| `build_bib.py` | Builds `Wzmn.bib` and assigns each paper to a CV section. |
| `rebuild_tex.py` | Updates `main.tex` in place. |
| `venues.py` | Loads `venues.yaml`. |
| `pipeline_state.py` | Content-hash step skipping. |
| `bib_utils.py` | Brace-counting BibTeX parser and text normalization. |
| `notify.py` | Failure notification (macOS Notification Center, GitHub Actions annotation). |
| `papers_fig.py`, `papers_graph.py` | Standalone figures. Not part of the pipeline; needs `requirements-figures.txt`. |

## How papers are matched across sources

Titles are not stable: BibTeX braces capitalization (`{B}aby{LM}`), Scholar
lowercases subtitles, papers get retitled between preprint and publication, and
the table is typed by hand. So matching is by **identifier**, and titles are only
a bootstrap:

1. **Stable ID.** Scholar's `citation_for_view` per paper; `externalIds` from
   Semantic Scholar for ArXiv/DOI/ACL/DBLP together — which is the crosswalk for
   when the ACL record knows no arXiv ID and vice versa. Stored in
   `identity.json` the first time it is seen.
2. **Normalized title.** Case, punctuation and BibTeX braces stripped. An
   identity claim, not a guess.
3. **Fuzzy title,** ≥0.90 automatically, ≥0.75 with confirmation, and only when
   it beats the runner-up by a clear margin. Every fuzzy match is listed in
   `WORKLIST.md`; confirming one and re-fetching binds its ID, after which it
   never needs judging again.

Anything ambiguous is reported, never guessed.

## GitHub ↔ Overleaf

`overleaf/` is a git submodule pointing at the Overleaf project. Step 7 pushes
`main.tex` and `Wzmn.bib` there, then pushes this repo to GitHub. If the Overleaf
remote has moved — because you edited the project in Overleaf's own editor — the
push rebases onto it and retries, so that no longer needs a manual pull.

Pull Overleaf-side edits back with `git -C overleaf pull origin`.

## Optional: clibib for the tail

[clibib](https://github.com/delip/clibib) resolves an identifier to BibTeX
through a Zotero translation server. `pip install clibib` and step 3 will use it
for **DOIs**, covering journals and book chapters that DBLP and the ACL Anthology
do not index. Without it the pipeline behaves exactly as before.

It is deliberately never used for title search. Measured against this repo's own
unresolved papers, its free-text lookup returned a confidently wrong paper 2
times in 5 with no error raised — "Reinforcement learning with large action
spaces for neural machine translation" came back as an unrelated Springer
proceedings volume. Its identifier paths are exact and fast, and its own README
recommends preferring them.

As a manual helper it is genuinely useful for `WORKLIST.md` items where you have
a DOI, ISBN, arXiv ID or publisher URL:

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

## Forking this for your own publications

```bash
git clone --recurse-submodules https://github.com/borgr/publications.git
cd publications
pip install -r requirements.txt

python init_new_author.py --overleaf-url https://git.overleaf.com/<your-project-id>
```

Then set `AUTHOR_NAME` and `SCHOLAR_USER_ID` in `config.py` — the pipeline
propagates the name into the BST files and `main.tex` on every run — and run
`python update.py`. Nothing else is author-specific.

`curl` and `git` must be on your PATH.

## Migrating from the spreadsheet

`papers.csv` replaced `Contributions_table.xlsx`. If you have an unmigrated
checkout, `read_table()` still falls back to the xlsx, and:

```bash
python scripts/migrate_to_csv.py --dry-run
python scripts/migrate_to_csv.py
```

converts it, verifying every value round-trips and reporting anything suspect.
CSV keeps the spreadsheet workflow while being diffable in git — which matters
because in the binary file six columns had rotted to empty unnoticed, one of them
(`inter\eval`) feeding a CV tag that consequently never rendered.
