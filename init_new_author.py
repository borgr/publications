#!/usr/bin/env python3
"""Reset the publications pipeline for a new author.

Wipes all personal data (papers, citations, generated bib/tex) and
replaces main.tex with the blank template so you can start fresh.

Usage:
    python init_new_author.py [--overleaf-url URL] [--yes]

Options:
    --overleaf-url URL   Git URL of your new Overleaf project.
                         If omitted, instructions are printed instead.
    --yes                Skip the confirmation prompt.

After running this script:
    1. Edit config.py to set your name and Google Scholar user ID.
    2. Run: python rebuild_tex.py   (generates Wzmn.bib and main.tex from scratch)
    3. Run: python update.py        (full Scholar fetch + push to Overleaf)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

FILE_DIR = os.path.dirname(os.path.abspath(__file__))

WIPE_FILES = {
    "citations.csv":          "per-paper citation counts",
    "profile_stats.json":     "Scholar profile stats (total citations, h-index)",
    "orig.bib":               "raw BibTeX entries",
    "papers.csv":             "the publications table",
    "overleaf/Wzmn.bib":      "generated bibliography",
    "resolve_attempts.json":  "resolve attempt counters",
    "identity.json":          "harvested paper identifiers",
    ".pipeline_state.json":   "step completion state",
    "WORKLIST.md":            "open items report",
    "tmp.csv":                "temporary Scholar fetch file",
}
XLSX_PATH    = os.path.join(FILE_DIR, "Contributions_table.xlsx")
PAPERS_CSV   = os.path.join(FILE_DIR, "papers.csv")
MAIN_TEX     = os.path.join(FILE_DIR, "overleaf", "main.tex")
TEMPLATE_TEX = os.path.join(FILE_DIR, "overleaf", "template.tex")


def _confirm(prompt: str) -> bool:
    return input(prompt + " [y/N] ").strip().lower() == "y"


def wipe_citations_csv():
    from citations_io import write_citation_rows
    path = os.path.join(FILE_DIR, "citations.csv")
    write_citation_rows([], path)   # header only, in the current format
    print("  Cleared citations.csv")


def wipe_profile_stats():
    path = os.path.join(FILE_DIR, "profile_stats.json")
    with open(path, "w") as f:
        json.dump({"citations": 0, "h_index": 0}, f)
    print("  Reset profile_stats.json")


def wipe_orig_bib():
    path = os.path.join(FILE_DIR, "orig.bib")
    open(path, "w").close()
    print("  Cleared orig.bib")


def wipe_wzmn_bib():
    path = os.path.join(FILE_DIR, "overleaf", "Wzmn.bib")
    if os.path.exists(path):
        open(path, "w").close()
        print("  Cleared overleaf/Wzmn.bib")


def wipe_tmp_csv():
    path = os.path.join(FILE_DIR, "tmp.csv")
    if os.path.exists(path):
        os.remove(path)
        print("  Deleted tmp.csv")


def wipe_resolve_attempts():
    # Machine-owned state files: deleting is enough, they are rebuilt on demand.
    for name in ("resolve_attempts.json", "identity.json",
                 ".pipeline_state.json", "WORKLIST.md"):
        path = os.path.join(FILE_DIR, name)
        if os.path.exists(path):
            os.remove(path)
            print(f"  Deleted {name}")


def wipe_contributions_xlsx():
    """Empty the publications table, keeping its columns.

    Handles both formats: papers.csv is the current source of truth, and the
    xlsx is cleared too so a fork that has not migrated starts clean either way.
    """
    if os.path.exists(PAPERS_CSV):
        import pandas as pd
        from table_io import write_table
        df = pd.read_csv(PAPERS_CSV, dtype=str, nrows=0)
        write_table(df, PAPERS_CSV)
        print("  Cleared papers.csv (columns preserved)")

    if os.path.exists(XLSX_PATH):
        import openpyxl
        wb = openpyxl.load_workbook(XLSX_PATH)
        ws = wb.active
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row - 1)
        wb.save(XLSX_PATH)
        print("  Cleared Contributions_table.xlsx (header row preserved)")


def reset_main_tex():
    if not os.path.exists(TEMPLATE_TEX):
        print("  WARNING: overleaf/template.tex not found; main.tex not reset")
        return
    shutil.copy2(TEMPLATE_TEX, MAIN_TEX)
    print("  Reset overleaf/main.tex from template.tex")


def replace_overleaf_submodule(overleaf_url: str):
    """Swap the overleaf/ git submodule to point to a new Overleaf project."""
    print(f"\nReplacing overleaf/ submodule → {overleaf_url}")
    cmds = [
        ["git", "-C", FILE_DIR, "submodule", "deinit", "-f", "overleaf"],
        ["git", "-C", FILE_DIR, "rm", "-f", "overleaf"],
        ["git", "-C", FILE_DIR, "submodule", "add", overleaf_url, "overleaf"],
    ]
    # Remove the cached submodule git dir if it exists
    modules_dir = os.path.join(FILE_DIR, ".git", "modules", "overleaf")
    if os.path.exists(modules_dir):
        shutil.rmtree(modules_dir)
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR running {' '.join(cmd[2:])}:\n{result.stderr.strip()}")
            return
    print("  overleaf/ now points to your Overleaf project")
    print("  Tip: run `git commit -m 'chore: switch to new Overleaf project'` to record this change")


def print_overleaf_instructions():
    print("""
  To connect your own Overleaf project:
    1. Create a new project on overleaf.com.
    2. In Overleaf: Menu → Git → copy the git clone URL.
    3. Run:
         git submodule deinit -f overleaf
         rm -rf .git/modules/overleaf
         git rm -f overleaf
         git submodule add <your-overleaf-git-url> overleaf
    4. Then run `python rebuild_tex.py` to populate overleaf/ with your files.
""")


def main():
    parser = argparse.ArgumentParser(
        description="Reset the publications pipeline for a new author",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--overleaf-url", metavar="URL",
                        help="Git URL of your new Overleaf project")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    print("This will erase all personal data from the pipeline:")
    print("  • citations.csv              (paper citation counts)")
    print("  • papers.csv                 (all paper entries)")
    print("  • identity.json, .pipeline_state.json, WORKLIST.md")
    print("  • orig.bib                   (raw BibTeX)")
    print("  • profile_stats.json         (Scholar totals)")
    print("  • overleaf/Wzmn.bib          (generated bibliography)")
    print("  • resolve_attempts.json      (resolve attempt counters)")
    print("  • overleaf/main.tex          → replaced with template.tex")
    if args.overleaf_url:
        print(f"  • overleaf/ submodule    → {args.overleaf_url}")
    print()

    if not args.yes and not _confirm("Proceed?"):
        print("Aborted.")
        sys.exit(0)

    wipe_citations_csv()
    wipe_profile_stats()
    wipe_orig_bib()
    wipe_wzmn_bib()
    wipe_tmp_csv()
    wipe_resolve_attempts()
    wipe_contributions_xlsx()
    reset_main_tex()

    if args.overleaf_url:
        replace_overleaf_submodule(args.overleaf_url)
    else:
        print_overleaf_instructions()

    print("\nDone. Next steps:")
    print("  1. Edit config.py  — set AUTHOR_NAME and SCHOLAR_USER_ID")
    print("  2. python rebuild_tex.py   — generate initial Wzmn.bib and main.tex")
    print("  3. python update.py        — fetch from Scholar and push to Overleaf")


if __name__ == "__main__":
    main()
