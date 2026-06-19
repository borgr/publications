# Publications Pipeline

Automated pipeline for maintaining a publications CV on Overleaf, backed by Google Scholar.

## How it works

```
Google Scholar
      │
      ▼
fetch_citations.py  →  citations.csv
                        profile_stats.json   (total citations, h-index)
      │
      ▼
update.py (orchestrates all steps)
      │
      ├─ Step 2: Contributions_table.xlsx   (add new papers)
      ├─ Step 3: orig.bib                   (resolve arXiv → published BibTeX)
      ├─ Step 4: overleaf/Wzmn.bib          (build final bibliography)
      ├─ Step 5: overleaf/main.tex          (update \nocite{} blocks + stats)
      └─ Step 6: git push → GitHub + Overleaf
```

Running `python update.py` from this directory does everything end-to-end.  
To only regenerate the bib and tex without fetching from Scholar, run `python rebuild_tex.py` directly.

## Installation

```bash
# 1. Clone the repo including the Overleaf submodule
git clone --recurse-submodules https://github.com/borgr/publications.git
cd publications

# If you already cloned without --recurse-submodules:
git submodule update --init

# 2. Install Python dependencies
pip install -r requirements.txt
```

`curl` must be available in your PATH (used for Scholar scraping — avoids Python TLS fingerprinting).  
`git` must be configured with credentials for both GitHub and Overleaf to enable the auto-push in step 6.

## GitHub ↔ Overleaf

The `overleaf/` directory is a **git submodule** pointing to the Overleaf project at  
`https://git.overleaf.com/67d33c3cba890bd614b76e93`.

- **GitHub** (`github.com/borgr/publications`) stores the pipeline code and the full history of all generated files including `overleaf/` as a submodule.
- **Overleaf** receives pushes directly to `overleaf/main.tex` and `overleaf/Wzmn.bib` every time the pipeline runs.

Changes made directly in the Overleaf editor can be pulled back with:
```bash
git -C overleaf pull origin
```

## Key files

### Pipeline (Python)

| File | Purpose |
|------|---------|
| `update.py` | Master script — runs all 6 steps, then commits and pushes |
| `fetch_citations.py` | Scrapes Google Scholar → `citations.csv` + `profile_stats.json` |
| `build_bib.py` | Reads `orig.bib` + `Contributions_table.xlsx` → `overleaf/Wzmn.bib` |
| `rebuild_tex.py` | Updates `\nocite{}` blocks and citations/h-index in `overleaf/main.tex` |
| `resolve_arxiv.py` | Resolves arXiv entries to published BibTeX via DBLP / ACL / S2 |
| `bib_utils.py` | Shared utilities: `read_df()`, `parse_bibtex()`, `normalize_text()` |

### Data

| File | Purpose |
|------|---------|
| `Contributions_table.xlsx` | Source of truth: venue, authors, year, bib key, category flags per paper |
| `orig.bib` | Raw BibTeX entries (manually curated + auto-resolved arXiv entries) |
| `citations.csv` | Per-paper citation counts scraped from Scholar |
| `profile_stats.json` | Total citations and h-index from Scholar profile |

### Overleaf (`overleaf/`)

| File | Purpose |
|------|---------|
| `main.tex` | The CV document — auto-updated by the pipeline |
| `Wzmn.bib` | Generated bibliography — do not edit by hand |
| `template.tex` | Documented starting point for adapting this setup |
| `planyr-rev.bst` | Bibliography style (reverse chronological, highlights author name) |

## Usage

```bash
# Full update (fetch Scholar, rebuild bib + tex, push to GitHub and Overleaf)
python update.py

# Dry run — show what would change without writing anything
python update.py --dry-run

# Skip the Scholar fetch (use existing citations.csv)
python update.py --skip-fetch

# Run without pushing to git remotes
python update.py --no-push

# Force all steps even if outputs are already up to date
python update.py --force
```

## Using this for your own publications

Run the reset script to wipe all personal data and start fresh:

```bash
# Without your own Overleaf project yet (instructions printed at the end)
python init_new_author.py

# With your Overleaf git URL (swaps the submodule automatically)
python init_new_author.py --overleaf-url https://git.overleaf.com/<your-project-id>
```

Then:
1. **Edit `config.py`** — set `AUTHOR_NAME` and `SCHOLAR_USER_ID`. The pipeline propagates the name into the BST files and `main.tex` on every run.
2. **Run `python rebuild_tex.py`** — generates initial `Wzmn.bib` and `main.tex`.
3. **Run `python update.py`** — fetches from Scholar and pushes to Overleaf.

## Citation count display

Each bib entry in `Wzmn.bib` includes a `citations={N}` field.  
The BST emits `\bibcitecount{N}` for each entry. Toggle display in `main.tex`:

```latex
%\newcommand{\bibcitecount}[1]{ \textit{\small[#1 cited]}}  % show
\newcommand{\bibcitecount}[1]{}                              % hide (default)
```
