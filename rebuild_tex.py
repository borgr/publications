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


class TexUpdateError(Exception):
    """A substitution in main.tex could not find what it was supposed to edit.

    Raised rather than warned about, because every one of these edits is the
    point of the step. A missing anchor means a CV section silently keeps the
    paper list it had last time, or the citation total silently goes stale, while
    the run reports success and pushes. main.tex is meant to be edited in the
    Overleaf editor, so reflowing one line used to be enough to freeze the
    Journals section permanently.
    """


def _replace_first_nocite_in_chapter(tex, chapter_title, new_keys):
    """Replace the first uncommented \\nocite{} inside a chapter block.

    Returns (tex, problem) where problem is None on success.
    """
    chapter_start = tex.find(r'\chapter*{' + chapter_title + '}')
    if chapter_start == -1:
        return tex, (f"chapter {chapter_title!r} not found — its paper list "
                     f"cannot be updated. Has the heading been renamed?")

    # Chapter ends at the next uncommented \putbib (anchored to start of line)
    putbib_match = re.search(r'^\\putbib', tex[chapter_start:], re.MULTILINE)
    chapter_end = chapter_start + putbib_match.end() if putbib_match else len(tex)
    chunk = tex[chapter_start:chapter_end]

    m = _NOCITE_RE.search(chunk)
    if not m:
        return tex, (f"no uncommented \\nocite{{}} inside chapter "
                     f"{chapter_title!r} — nothing to replace")

    new_chunk = chunk[:m.start()] + _nocite_str(new_keys) + chunk[m.end():]
    return tex[:chapter_start] + new_chunk + tex[chapter_end:], None


def _replace_nocite_after_comment(tex, comment_text, new_keys):
    """Replace the first uncommented \\nocite{} following comment_text.

    Returns (tex, problem) where problem is None on success.
    """
    comment_pos = tex.find(comment_text)
    if comment_pos == -1:
        return tex, (f"marker {comment_text!r} not found — the list it labels "
                     f"cannot be updated")

    after = tex[comment_pos + len(comment_text):]
    m = _NOCITE_RE.search(after)
    if not m:
        return tex, f"no uncommented \\nocite{{}} after {comment_text!r}"

    abs_start = comment_pos + len(comment_text) + m.start()
    abs_end = comment_pos + len(comment_text) + m.end()
    return tex[:abs_start] + _nocite_str(new_keys) + tex[abs_end:], None


_STATS_RE   = re.compile(r'\\textbf\{Citations\t(\d+)\nh-index\t(\d+)\n\}')
_AUTHOR_LINE_RE = re.compile(r'(\\noindent\\today\n\n?)([^\n]+)(\n\\textbf\{Citations)', re.MULTILINE)


def patch_bst_author(author_name: str = None) -> list:
    """Point the BST files' name-bolding at config.AUTHOR_NAME.

    Returns a list of problems. Not fatal -- the CV still compiles, it just
    bolds the wrong name or nobody -- but it must be visible, because the failure
    is invisible in every other way: the run succeeds and only the rendered PDF
    is wrong.

    Verifies afterwards rather than trusting the substitution, which is what
    caught two silent no-ops: a name the finder could not re-match (renaming away
    from "Jean-Paul Sartre" did nothing) and a BST with no recognisable pattern.
    """
    if author_name is None:
        author_name = config.AUTHOR_NAME
    parts = author_name.split()
    author_first, author_last = (parts[0], parts[-1]) if parts else ("", "")

    problems = []
    for bst_name in _BST_FILES:
        bst_path = os.path.join(OVERLEAF_DIR, bst_name)
        if not os.path.exists(bst_path):
            continue
        with open(bst_path) as f:
            original = f.read()

        patched = original
        found = _find_author_names_in_bst(patched)
        for old_name in found:
            if old_name == author_name:
                continue
            # Everywhere, not only where the name is the whole quoted value. The
            # name also appears *inside* the strings the style emits --
            # `"\textbf{\emph{Old Name}\textsuperscript{st}}"` -- and replacing
            # only `"Old Name"` left those, so a fork's bibliography printed the
            # original author's name in bold on every one of the fork's papers.
            patched = patched.replace(old_name, author_name)
            old_parts = old_name.split()
            for old_part, new_part in ((old_parts[0], author_first),
                                       (old_parts[-1], author_last)):
                patched = patched.replace(
                    f'format.name$ purify$ "{old_part}" =',
                    f'format.name$ purify$ "{new_part}" =')

        if patched != original:
            with open(bst_path, "w") as f:
                f.write(patched)
            print(f"  Patched author name in {bst_name}")

        # Verify rather than trust: the configured name must be present, and no
        # previous author's name may remain anywhere in the file.
        if f'"{author_name}"' not in patched:
            problems.append(
                f"{bst_name} does not reference {author_name!r}, so your name "
                f"will not be bolded in the bibliography"
                + (f" (it still names {found[0]!r})" if found else
                   " (no `t \"Name\" =` comparison found to replace)"))
        # Any trace of a previous author's name -- full, or first/last as a whole
        # word. `iclr-based.bst` carries a `format.name.bold` that names the parts
        # as bare identifiers, which no quoted-string replacement reaches.
        stragglers = set()
        for old_name in found:
            if old_name == author_name:
                continue
            if old_name in patched:
                stragglers.add(old_name)
            for part in {old_name.split()[0], old_name.split()[-1]}:
                if len(part) > 3 and re.search(r'\b' + re.escape(part) + r'\b',
                                               patched):
                    stragglers.add(part)
        if stragglers:
            problems.append(
                f"{bst_name} still mentions {sorted(stragglers)} after renaming "
                f"to {author_name!r} — that style would bold the wrong name. "
                f"main.tex selects its style with \\defaultbibliographystyle, so "
                f"this only matters if you switch to it.")
    return problems


# BibTeX keywords that appear in the same comparison position as a name.
_BST_NON_NAMES = {"others", "et al.", "and others"}


def _find_author_names_in_bst(bst_text: str) -> list:
    """Author names used in the BST's name comparisons (`t "Name" =`).

    Deliberately permissive about name shape. The previous pattern was
    `[A-Z][a-z]+ [A-Z][a-z]+`, which cannot match a hyphenated, accented,
    single-word or three-part name -- so once the file had been renamed to one of
    those, every later rename silently did nothing. `"others"` and friends sit in
    the same position and are excluded by name.
    """
    return [n for n in re.findall(r't\s+"([^"]{2,80})"\s*=', bst_text)
            if n.strip().lower() not in _BST_NON_NAMES]


def _update_author_name_in_tex(tex: str):
    """Set the author name line between \\noindent\\today and \\textbf{Citations."""
    author_name = config.AUTHOR_NAME
    m = _AUTHOR_LINE_RE.search(tex)
    if not m:
        return tex, ("the author-name line (between \\noindent\\today and the "
                     "Citations block) was not found")
    if m.group(2) == author_name:
        return tex, None
    # A callable replacement: a name containing a backslash would otherwise be
    # parsed as a regex template.
    return _AUTHOR_LINE_RE.sub(
        lambda mm: mm.group(1) + author_name + mm.group(3), tex), None


def _update_profile_stats(tex: str):
    """Set the Citations/h-index numbers from profile_stats.json."""
    if not os.path.exists(STATS_PATH):
        # Genuinely optional: no stats fetched yet, nothing to write.
        return tex, None
    try:
        with open(STATS_PATH) as f:
            stats = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return tex, f"could not read {os.path.basename(STATS_PATH)}: {e}"

    citations = stats.get("citations")
    h_index   = stats.get("h_index")
    if citations is None or h_index is None:
        return tex, "profile_stats.json has no 'citations'/'h_index'"

    if not _STATS_RE.search(tex):
        return tex, ("the Citations/h-index block was not found, so the totals "
                     "in the CV would silently stay stale. The expected shape is "
                     "\\textbf{Citations<TAB>N<newline>h-index<TAB>N<newline>}")
    new_tex = _STATS_RE.sub(
        lambda _m: f'\\textbf{{Citations\t{citations}\nh-index\t{h_index}\n}}', tex)
    if new_tex == tex:
        print(f"  Citations/h-index already current ({citations}, {h_index})")
    return new_tex, None


# Each CV section and how its \nocite{} list is located. Data rather than a
# sequence of calls, so the set is visible in one place and every one is checked.
_SECTIONS = (
    # (BibCategories field, kind, anchor)
    ("journals",    "chapter", "Refereed Articles"),
    ("conferences", "comment", "% Conferences:"),
    ("reviews",     "chapter", "Review Papers"),
    ("workshops",   "chapter", "Workshop Papers"),
    ("drafts",      "chapter", "Non-reviewed or under review papers"),
)


def update_tex(tex, cats: BibCategories):
    """Apply every edit. Returns (tex, problems); problems is empty on success."""
    problems = []

    tex, problem = _update_author_name_in_tex(tex)
    if problem:
        problems.append(problem)

    for field, kind, anchor in _SECTIONS:
        keys = getattr(cats, field)
        if kind == "chapter":
            tex, problem = _replace_first_nocite_in_chapter(tex, anchor, keys)
        else:
            tex, problem = _replace_nocite_after_comment(tex, anchor, keys)
        if problem:
            problems.append(f"{field}: {problem}")

    tex, problem = _update_profile_stats(tex)
    if problem:
        problems.append(problem)

    return tex, problems


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
    # Name the template that is actually present. The submodule copy only exists
    # while overleaf/ points at the original author's project, so naming it
    # unconditionally gave a fork a command that could not work.
    template = os.path.join(OVERLEAF_DIR, "template.tex")
    if not os.path.exists(template):
        template = os.path.join(FILE_DIR, "templates", "main.tex")
    return (f"{TEX_PATH} is missing, but overleaf/ has other files.\n"
            f"  If this is a new Overleaf project, seed it:  "
            f"cp {template} {TEX_PATH}")


def main(cats: BibCategories = None):
    """Update overleaf/main.tex. If cats is provided, skip re-running build_bib."""
    problem = check_overleaf_present()
    if problem:
        raise FileNotFoundError(problem)

    for bst_problem in patch_bst_author():
        print(f"  Warning: {bst_problem}")

    if cats is None:
        cats = build_bib.main()

    with open(TEX_PATH) as f:
        tex = f.read()

    tex, problems = update_tex(tex, cats)
    if problems:
        # Refuse to write a half-updated CV. Every one of these edits is the
        # point of this step; a partial success leaves a section frozen with a
        # stale paper list, which is worse than not running.
        raise TexUpdateError(
            f"{len(problems)} edit(s) to {os.path.basename(TEX_PATH)} could not be "
            f"applied, so it was left untouched:\n  - "
            + "\n  - ".join(problems)
            + "\n  main.tex is meant to be edited in Overleaf, so an anchor may "
              "have been reflowed or renamed. Restore the anchor, or update "
              "_SECTIONS / the regexes in rebuild_tex.py to match.")

    with open(TEX_PATH, "w") as f:
        f.write(tex)

    print(f"\nUpdated {TEX_PATH}")


if __name__ == "__main__":
    main()
