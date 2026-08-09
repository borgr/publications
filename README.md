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
python update.py --force      # ignore the "nothing changed" checks
python update.py --no-push    # build locally, leave the remotes alone
python update.py --skip-fetch # reuse the citation counts already on disk
```

### What it does

Each step is skipped when the **contents** of both its inputs and its outputs are
unchanged since it last succeeded (recorded in `.pipeline_state.json`) — content
hashes, not mtimes, so a fresh clone behaves correctly and a no-op rewrite does
not cascade. Checking the outputs too is what makes a hand-edit heal: revert the
citation totals in `main.tex` and the next run notices its own output is gone and
rebuilds it, instead of skipping forever because no input changed.

A run where a source never replied does not record its step as done, so the next
run asks again rather than freezing a lookup that failed for network reasons.

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
python scripts/prune_bib.py         # list orig.bib entries nothing refers to (--apply to remove)
python scripts/refresh_venues.py    # refresh venue rankings and impact metrics
python -m pytest tests/ -q          # the test suite
```

## Once, when setting up

```bash
python scripts/install_schedule.py       # weekly local run (macOS launchd)
python scripts/install_schedule.py --show  # print the plist / cron line instead
python scripts/install_overleaf_credential.py  # store the Overleaf git token, so step 7 can push

python init_new_author.py                # wipe personal data, for a fork
```

The step scripts (`fetch_citations.py`, `build_bib.py`, `rebuild_tex.py`,
`resolve_arxiv.py`) also run standalone, which is useful when debugging one stage.

## Setting it up

```bash
git clone https://github.com/borgr/publications.git
cd publications
pip install -r requirements.txt
python init_new_author.py --overleaf-url <your-overleaf-git-url>
```

Note the plain `clone`: **not** `--recurse-submodules`. `overleaf/` is a
submodule pointing at the original author's Overleaf project, which you have no
credentials for, so recursing aborts the whole clone with `could not read
Password`. `init_new_author.py` repoints it at yours and clears the personal
data; run it with `--overleaf-url` and it does both in one step. Until then the
`overleaf/` directory is simply empty, and every step except the push works.

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

- **Locally**, weekly (Monday 08:37), via `scripts/install_schedule.py`. This is
  the one that fetches from Scholar. It also needs the Overleaf token stored once,
  with `scripts/install_overleaf_credential.py` — see below.
- **[GitHub Actions](.github/workflows/ci.yml)** covers what does not need
  Scholar: the test suite on three Python versions, the oldest supported
  dependency versions, a rebuild from committed data, a determinism check, a
  fork-from-scratch check, and a failure if the table has duplicate rows or an
  ambiguous citation join. No secrets, works in a fork. It also runs weekly, on
  Monday at 14:47 UTC — deliberately *after* the local run, since checking first
  would compare against data an hour from being replaced.

### Letting the local run push to Overleaf

Step 7 pushes with plain `git push`, so git has to be able to authenticate on its
own — there is nobody at the keyboard at 08:37 on a Monday. Run this once, and
again whenever you rotate the token:

```bash
python scripts/install_overleaf_credential.py
```

It takes the URL from the first of three places that has one:

1. `~/.config/publications/overleaf_git_url`, a one-line hand-over file. Use this
   when the person holding the token and the person running the installer are not
   the same, or not at the same keyboard. It is read, transferred into the
   credential store, verified, and then **deleted**; a token it could not
   authenticate with is left in place so you can correct it. It lives outside the
   repository deliberately, and the installer refuses to read a path inside the
   repository rather than risk `git add -A` committing a token to a public remote.
2. `$OVERLEAF_GIT_URL`, if you have already exported it.
3. A hidden prompt.

From there it goes to git's own credential store — the macOS keychain, via
`git credential approve` — and a real request proves it works. The token is never
written into the working tree, never put on a command line, and never printed;
storing it in the submodule's remote URL or in `.git/config` would leave it in
plaintext, and a dotfile in the tree is one `git add -A` away from a public repo.

Until you do this, `update.py` prints the reason it will not be able to push
*before* step 1 rather than after a full Scholar fetch, and then **runs anyway**.
Only the push is lost: `papers.csv`, `citations.csv` and the rebuilt CV all end up
current on disk and committed locally, and step 7 fails, notifies and exits
non-zero on its own. Tokens get revoked and rotated, and one that froze the whole
pipeline would quietly stop the data tracking reality as well — a worse failure
than a stale Overleaf. `GIT_TERMINAL_PROMPT=0` keeps a missing credential from
stalling on a password prompt an unattended run has no way to answer.

### Letting CI see your Overleaf project (optional)

Without this, nothing outside your own machine can tell whether the CV Overleaf
compiles matches the committed data — the failure that motivated it was a run of
`--no-push` runs that left Overleaf hundreds of citations behind while every run
reported success.

1. In Overleaf: **Account Settings → Git integration** for a token, and
   **Menu → Git** for the project URL. They are separate — the URL Overleaf
   shows you carries no credential, and cloning it in CI fails asking for a
   password.
2. Combine them into one URL with the token as the password, and store that as
   a GitHub repository secret named `OVERLEAF_GIT_URL` (**Settings → Secrets
   and variables → Actions → New repository secret**):

   ```
   https://git:YOUR_TOKEN@git.overleaf.com/YOUR_PROJECT_ID
   ```

   Use a token generated for this, not whatever your local clone
   authenticates with, so revoking CI's access does not break your own pushes.

CI then rebuilds the CV from the committed data and **fails if Overleaf would
compile something different**. To have CI push the fix rather than just report
it, add a repository *variable* `PUBLISH_TO_OVERLEAF` set to `true`. That is off
by default: Overleaf is a document you also edit by hand, so writing to it is a
decision, not a default. It rebases before retrying, so a push and a hand edit
racing does not fail the run.

Publishing runs on the default branch only, and one run at a time. Overleaf holds
one CV, so a work-in-progress branch has nowhere good to put its version of it:
publishing would overwrite the real one, and failing would go red for exactly the
work the branch exists to do. Two runs pushing at once is the other half of that —
the job takes a `concurrency` lock rather than racing.

With no secret set, the job prints how to enable itself and passes — a fork is
never red for a project it does not have. The same staleness is reported locally
in `WORKLIST.md`, which needs no credentials.

#### What a fork can and cannot see

The token is never in the repository, so forking this project cannot hand
anybody your Overleaf account. Concretely:

- **Nothing committed carries a credential.** `tests/test_no_secrets.py` fails
  the build if one ever is, so this is checked rather than asserted.
- **Secrets do not travel with a fork.** They live in your repository's
  settings, encrypted, and GitHub will not read one back to you either — only
  overwrite it. A fork starts with none, which is why the job is written to pass
  when it finds none.
- **A pull request from a fork gets no secrets**, by GitHub's design, so a PR
  that edits the workflow to print the token cannot: the job is also skipped on
  `pull_request` outright.
- **Logs are scrubbed.** GitHub masks any registered secret's value in output,
  the URL is passed only through the environment, and the clone step redacts
  `//…@` out of git's own error text before printing it.

What *is* public is the project **URL** in `.gitmodules`
(`https://git@git.overleaf.com/<project-id>`). That is an address, not a key:
cloning it without the token fails, which is exactly what the plain-`clone`
instruction above is about.

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
identifiers), `resolve_attempts.json` (retry counters), `.pipeline_state.json`
(the input and output hashes each step last saw), `WORKLIST.md`,
`overleaf/Wzmn.bib`.

`papers.csv` is the only table format. `table_io.py` addresses every column by
header name — so inserting or reordering one cannot misfile a value — and
validates the whole table on every load, because a spreadsheet round-trip that
reformats a column produces a table that still builds, just wrongly.

### Code

| File | Purpose |
|------|---------|
| `update.py` | The orchestrator. |
| `fetch_citations.py` | Scholar scraper, including each paper's stable `citation_for_view` ID. |
| `table_io.py` | Reads/validates/writes `papers.csv`, by column name. |
| `identity.py` | Stable identifiers, and the joins built on them. |
| `citations_io.py` | `citations.csv` reading and writing. |
| `resolve_arxiv.py` | arXiv → published BibTeX, via DBLP / S2 / ACL / OpenReview / DOI / OpenAlex. Finds candidates; writes nothing. |
| `bib_edit.py` | Decides what may change in `orig.bib` and writes it: keys, the venue transplant, the in-place update. |
| `build_bib.py` | Builds `Wzmn.bib` and assigns each paper to a CV section. |
| `rebuild_tex.py` | Updates `main.tex` in place. |
| `bib_utils.py` | Brace-counting BibTeX parser, text normalization, publication ranking. Reads only. |
| `venues.py` | Loads `venues.yaml`. |
| `pipeline_state.py` | Content-hash step skipping, over inputs and outputs both. |
| `notify.py` | Failure notification (macOS Notification Center, Actions annotation). |
| `scripts/papers_fig.py`, `scripts/papers_graph.py` | Standalone figures. Not part of the pipeline; needs `requirements-figures.txt`. |

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

A mis-resolution is a different problem from a duplicate, and is never fixed
automatically: a duplicate is provable and safe to drop, while an entry pointing at
the *wrong* paper has to be looked at. The cheap test is whether the entry credits
the author at all — `bib_utils.lists_author` — which is what step 3 applies to
every candidate and what `tests/test_author_on_every_paper.py` asserts over the
whole table. Without it the resolver accepted an invited talk by one of a Nature
paper's twenty co-authors as that paper's published version, on a title similarity
of 0.86, and the CV printed it.

## Pruning orig.bib

`orig.bib` accumulates: a paper's arXiv entry stays behind when step 3 moves its row
to the published key. Sixty-nine of a hundred and seventy-eight entries were
unreachable that way. None of it reaches the CV — `Wzmn.bib` is built from the
intersection of the table and the bibliography — so this is about being able to read
the file and see a real diff in it.

`scripts/prune_bib.py` reports them, and removes them with `--apply`. Three things
protect an entry, and any one is enough: a table row's `Bib` key, a `\nocite` in
`main.tex` (including a commented-out one), or a Scholar ID bound to it in
`identity.json`. It also strips `pretitle` fields committed back into the source,
which every build regenerates from `papers.csv` anyway. Git history is the backup.

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
