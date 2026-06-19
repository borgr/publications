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

TEX_PATH   = os.path.join(FILE_DIR, "overleaf", "main.tex")
STATS_PATH = os.path.join(FILE_DIR, "profile_stats.json")

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


_STATS_RE = re.compile(r'\\textbf\{Citations\t(\d+)\nh-index\t(\d+)\n\}')


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


def main(cats: BibCategories = None):
    """Update example.tex. If cats is provided, skip re-running build_bib."""
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
