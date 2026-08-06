#!/usr/bin/env python3
"""Rebuild overleaf/main.tex and overleaf/Wzmn.bib with the latest publication data.

Calls build_bib.main() to produce Wzmn.bib, then uses the returned
bib key lists to update the \\nocite{} blocks in main.tex in-place.
"""

import json
import os
import re
import sys

FILE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FILE_DIR)

import build_bib
from build_bib import BibCategories
import config

TEX_PATH      = os.path.join(FILE_DIR, "overleaf", "main.tex")
STATS_PATH    = os.path.join(FILE_DIR, "profile_stats.json")
OVERLEAF_DIR  = os.path.join(FILE_DIR, "overleaf")
_BST_FILES    = ["planyr-rev.bst", "planyr.bst", "iclr-based.bst"]

# Matches \nocite{...} only at the start of a line (skips commented-out % \nocite lines).
_NOCITE_RE = re.compile(r'^\\nocite\{[^}]*\}', re.MULTILINE)


def _nocite_str(keys):
    return r'\nocite{' + ','.join(keys) + '}'


def _replace_first_nocite_in_chapter(tex, chapter_title, new_keys):
    """Replace the first uncommented \\nocite{} inside a chapter block."""
    chapter_start = tex.find(r'\chapter*{' + chapter_title + '}')
    if chapter_start == -1:
        print(f"Warning: chapter {chapter_title!r} not found in tex")
        return tex

    # Chapter ends at the next uncommented \putbib (anchored to start of line)
    putbib_match = re.search(r'^\\putbib', tex[chapter_start:], re.MULTILINE)
    chapter_end = chapter_start + putbib_match.end() if putbib_match else len(tex)
    chunk = tex[chapter_start:chapter_end]

    m = _NOCITE_RE.search(chunk)
    if not m:
        print(f"Warning: no \\nocite{{}} found in chapter {chapter_title!r}")
        return tex

    new_chunk = chunk[:m.start()] + _nocite_str(new_keys) + chunk[m.end():]
    return tex[:chapter_start] + new_chunk + tex[chapter_end:]


def _replace_nocite_after_comment(tex, comment_text, new_keys):
    """Replace the first uncommented \\nocite{} that follows comment_text."""
    comment_pos = tex.find(comment_text)
    if comment_pos == -1:
        print(f"Warning: comment {comment_text!r} not found in tex")
        return tex

    after = tex[comment_pos + len(comment_text):]
    m = _NOCITE_RE.search(after)
    if not m:
        print(f"Warning: no \\nocite{{}} found after comment {comment_text!r}")
        return tex

    abs_start = comment_pos + len(comment_text) + m.start()
    abs_end = comment_pos + len(comment_text) + m.end()
    return tex[:abs_start] + _nocite_str(new_keys) + tex[abs_end:]


_STATS_RE   = re.compile(r'\\textbf\{Citations\t(\d+)\nh-index\t(\d+)\n\}')
_AUTHOR_LINE_RE = re.compile(r'(\\noindent\\today\n\n?)([^\n]+)(\n\\textbf\{Citations)', re.MULTILINE)


def patch_bst_author(author_name: str = None) -> None:
    """Replace hardcoded author name in all BST files in overleaf/ from config."""
    if author_name is None:
        author_name = config.AUTHOR_NAME
    author_first = author_name.split()[0]
    author_last  = author_name.split()[-1]
    for bst_name in _BST_FILES:
        bst_path = os.path.join(OVERLEAF_DIR, bst_name)
        if not os.path.exists(bst_path):
            continue
        with open(bst_path) as f:
            original = f.read()
        patched = original
        # Replace any full-name string literal (catches comparisons and formatted output)
        for old_name in _find_author_names_in_bst(patched):
            patched = patched.replace(f'"{old_name}"', f'"{author_name}"')
            # Also fix first/last in format.name$ purify$ comparisons
            old_first = old_name.split()[0]
            old_last  = old_name.split()[-1]
            patched = patched.replace(
                f'format.name$ purify$ "{old_first}" =',
                f'format.name$ purify$ "{author_first}" ='
            )
            patched = patched.replace(
                f'format.name$ purify$ "{old_last}" =',
                f'format.name$ purify$ "{author_last}" ='
            )
        if patched != original:
            with open(bst_path, "w") as f:
                f.write(patched)
            print(f"  Patched author name in {bst_name}")


def _find_author_names_in_bst(bst_text: str) -> list:
    """Extract author full-name strings used in name comparisons (t "Name" = pattern)."""
    return re.findall(r't "([A-Z][a-z]+ [A-Z][a-z]+)" =', bst_text)


def _update_author_name_in_tex(tex: str) -> str:
    """Replace the author name line between \\noindent\\today and \\textbf{Citations."""
    author_name = config.AUTHOR_NAME
    m = _AUTHOR_LINE_RE.search(tex)
    if not m:
        return tex
    if m.group(2) == author_name:
        return tex
    return _AUTHOR_LINE_RE.sub(r'\g<1>' + author_name + r'\g<3>', tex)


def _update_profile_stats(tex: str) -> str:
    """Replace the Citations/h-index numbers using profile_stats.json if present."""
    if not os.path.exists(STATS_PATH):
        return tex
    try:
        with open(STATS_PATH) as f:
            stats = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not read {STATS_PATH}: {e}")
        return tex

    citations = stats.get("citations")
    h_index   = stats.get("h_index")
    if citations is None or h_index is None:
        print(f"Warning: profile_stats.json missing 'citations' or 'h_index'")
        return tex

    if not _STATS_RE.search(tex):
        print("Warning: could not find Citations/h-index block in tex to update")
        return tex
    new_tex = _STATS_RE.sub(
        f'\\\\textbf{{Citations\t{citations}\nh-index\t{h_index}\n}}', tex
    )
    if new_tex == tex:
        print(f"  Citations/h-index already current ({citations}, {h_index})")
    return new_tex


def update_tex(tex, cats: BibCategories):
    tex = _update_author_name_in_tex(tex)
    # Refereed Articles: first uncommented nocite = journals
    tex = _replace_first_nocite_in_chapter(tex, "Refereed Articles", cats.journals)
    # Refereed Articles: nocite after "% Conferences:" comment = conferences
    tex = _replace_nocite_after_comment(tex, "% Conferences:", cats.conferences)
    # Remaining chapters each have a single nocite
    tex = _replace_first_nocite_in_chapter(tex, "Review Papers", cats.reviews)
    tex = _replace_first_nocite_in_chapter(tex, "Workshop Papers", cats.workshops)
    tex = _replace_first_nocite_in_chapter(tex, "Non-reviewed or under review papers", cats.drafts)
    tex = _update_profile_stats(tex)
    return tex


def check_overleaf_present() -> str:
    """Return "" if overleaf/main.tex is usable, else what to do about it.

    `overleaf/` is a git submodule, so a clone without `--recurse-submodules`
    leaves it empty and this step died on a bare FileNotFoundError five steps
    into a run. Saying so plainly is the difference between a two-second fix and
    a confusing one.
    """
    if os.path.exists(TEX_PATH):
        return ""
    if not os.path.isdir(OVERLEAF_DIR) or not os.listdir(OVERLEAF_DIR):
        return (f"overleaf/ is empty — it is a git submodule that was not checked "
                f"out.\n  Fix with:  git submodule update --init\n"
                f"  For a fork, point it at your own project:  "
                f"python init_new_author.py --overleaf-url <your-overleaf-git-url>")
    return (f"{TEX_PATH} is missing, but overleaf/ has other files.\n"
            f"  If this is a new Overleaf project, seed it:  "
            f"cp {os.path.join(OVERLEAF_DIR, 'template.tex')} {TEX_PATH}")


def main(cats: BibCategories = None):
    """Update overleaf/main.tex. If cats is provided, skip re-running build_bib."""
    problem = check_overleaf_present()
    if problem:
        raise FileNotFoundError(problem)

    patch_bst_author()

    if cats is None:
        cats = build_bib.main()

    with open(TEX_PATH) as f:
        tex = f.read()

    tex = update_tex(tex, cats)

    with open(TEX_PATH, "w") as f:
        f.write(tex)

    print(f"\nUpdated {TEX_PATH}")


if __name__ == "__main__":
    main()
