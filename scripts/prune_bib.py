#!/usr/bin/env python3
"""Remove BibTeX entries that no table row refers to.

    python scripts/prune_bib.py            # report
    python scripts/prune_bib.py --apply    # remove them

orig.bib accumulates. An entry arrives when a paper is added, and another arrives
when step 3 finds its published version under a different key; the row moves to the
new key and the old entry stays. Sixty-nine of a hundred and seventy-eight entries
were unreachable this way. None of them can reach the CV -- Wzmn.bib is built from
the intersection of the table and the bibliography -- so this is clutter rather than
a correctness problem, and pruning it is about being able to read the file and see a
real diff in it.

Three things make an entry reachable, and any one of them protects it:

  a table row's Bib key    the ordinary case
  a \\nocite in main.tex    including a commented-out one, which someone may
                           uncomment; the CV's own \\nocite blocks are regenerated
                           from the table, but hand-written ones are not
  a Scholar ID in the      deleting the entry would leave the binding pointing at
  identity store           nothing, and the citations would land nowhere

Deleted entries stay in git history, which is the actual backup here.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bib_utils import extract_field, parse_bibtex
from identity import IdentityStore
from table_io import read_table

BIB_PATH = os.path.join(ROOT, "orig.bib")
TEX_PATH = os.path.join(ROOT, "overleaf", "main.tex")


def _cited_in_tex(keys, tex_path=TEX_PATH):
    """Keys that appear inside a brace-delimited citation list in main.tex.

    Substring matching would be wrong both ways: `wang2026mindgames` occurs inside
    `wang2026mindgameslivearenaevaluating`, so a bare `in` test protects entries
    nothing cites and, worse, reports the wrong one as protected.
    """
    try:
        with open(tex_path, encoding="utf-8") as fh:
            tex = fh.read()
    except OSError:
        return set()
    return {k for k in keys
            if re.search(r'[{,]\s*' + re.escape(k) + r'\s*[,}]', tex)}


def plan(bib_text, table_keys, store, tex_path=TEX_PATH):
    """Return (removable, protected) as lists of (key, title, reason)."""
    entries = parse_bibtex(bib_text)
    unreferenced = [e for e in entries if e["item_name"] not in table_keys]
    cited = _cited_in_tex({e["item_name"] for e in unreferenced}, tex_path)

    removable, protected = [], []
    for entry in unreferenced:
        key = entry["item_name"]
        title = (extract_field(entry["content"], "title") or "").strip()
        if key in cited:
            protected.append((key, title, "cited in main.tex"))
        elif (store.records.get(key) or {}).get("scholar_id"):
            protected.append((key, title, "a Scholar ID is bound to it"))
        else:
            removable.append((key, title, "no table row refers to it"))
    return removable, protected


def prune(bib_text, keys):
    """Return bib_text with `keys`' entries cut out.

    Locates each entry by the `beg + rest` text `parse_bibtex` already extracted,
    which is documented to reconstruct the source exactly. A second brace parser
    written here would be a second place to get a title containing `}` wrong.
    """
    out = bib_text
    for entry in parse_bibtex(bib_text):
        if entry["item_name"] not in keys:
            continue
        source = entry["beg"] + entry["rest"]
        start = out.find(source)
        if start == -1:
            continue
        end = start + len(source)
        # Take the newlines that followed the entry with it. The blank line that
        # *preceded* it belongs to the entry before, and is what keeps the survivors
        # separated -- consuming both ends would run two entries together.
        while end < len(out) and out[end] in "\r\n":
            end += 1
        out = out[:start] + out[end:]
    return out.rstrip("\n") + "\n"


def strip_generated_fields(bib_text):
    """Return (text, n_stripped) with generated `pretitle` fields removed.

    `pretitle` holds the tag macros (\\LANG, \\META) that build_bib injects into
    Wzmn.bib from the table's tag columns. Fifty-six entries had one committed back
    into orig.bib, where it is dead weight: every build strips it and regenerates it
    from the row, so the copy in the source can only ever be a stale duplicate of
    what papers.csv says.

    Reuses build_bib's own stripper, so a field it can remove and a field this can
    remove cannot drift apart.
    """
    import build_bib

    out, stripped = bib_text, 0
    for entry in parse_bibtex(bib_text):
        source = entry["beg"] + entry["rest"]
        cleaned = entry["beg"] + build_bib.remove_pretitle_tags(entry["rest"])
        if cleaned != source and source in out:
            out = out.replace(source, cleaned, 1)
            stripped += 1
    return out, stripped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="rewrite orig.bib (default: report only)")
    args = parser.parse_args(argv)

    with open(BIB_PATH, encoding="utf-8") as fh:
        bib_text = fh.read()
    table_keys = {str(k).strip() for k in read_table()["Bib"].dropna()}
    table_keys -= {"", "nan", "none", "None"}

    removable, protected = plan(bib_text, table_keys, IdentityStore.load())
    _stripped_text, n_stripped = strip_generated_fields(bib_text)

    if protected:
        print(f"{len(protected)} unreferenced entry(ies) kept:")
        for key, title, reason in protected:
            print(f"  [{key:<44}] {reason}\n      {title[:66]}")
        print()

    if removable:
        print(f"{len(removable)} entry(ies) no table row refers to:")
        for key, title, _reason in removable:
            print(f"  [{key:<44}] {title[:66]}")
    if n_stripped:
        print(f"\n{n_stripped} entry(ies) carry a generated `pretitle` field, which "
              f"every build strips and rewrites from papers.csv.")
    if not (removable or n_stripped):
        print("Nothing to clean: every entry is reachable and carries no generated "
              "fields.")
        return 0

    if not args.apply:
        print("\n(report only: orig.bib not modified. Re-run with --apply.)")
        return 0

    pruned = prune(bib_text, {k for k, _, _ in removable})
    pruned, _ = strip_generated_fields(pruned)
    kept = len(parse_bibtex(pruned))
    if kept != len(parse_bibtex(bib_text)) - len(removable):
        print("Refusing to write: the rewritten file does not have the expected "
              "number of entries.")
        return 1
    with open(BIB_PATH, "w", encoding="utf-8") as fh:
        fh.write(pruned)
    print(f"\norig.bib now has {kept} entry(ies): removed {len(removable)}, "
          f"stripped {n_stripped} generated field(s). Both remain in git history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
