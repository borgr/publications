import difflib
import os
import re
from datetime import datetime, timedelta
from typing import NamedTuple

import pandas as pd

from bib_utils import read_df, parse_bibtex, normalize_text

FILE_DIR = os.path.dirname(os.path.abspath(__file__))

RELEVANT_TAGS = {
    r"inter\eval": "\\UND",
    "Enabling Low Budget Research": "\\META",
    "Open": "\\COL",
    "Language&Cognition": "\\LANG",
}

JOURNALS = {"jml", "tacl", "nature", "nature machine intelligence", "tmlr"}

VENUE_DESCRIPTIONS = {
    "jml": "Top Linguistic journal with an impact factor 4.014",
    "tacl": '11th out of 145 journals in the "computer science (artificial intelligence)" category, with an impact factor of 10.9',
    "nature": "The top venue in the world under many metrics, with an impact factor of 50.5",
    "nature machine intelligence": "Journal with an 18.8 impact factor, higher than the top journals and conferences in Main Machine Learning venues.",
    "conll": "11th of 20 in computational linguistics conferences by Google Scholar",
    "acl": "1th of 20 in computational linguistics conferences by Google Scholar",
    "eacl": "6th of 20 in computational linguistics conferences by Google Scholar",
    "emnlp": "1th of 20 in computational linguistics conferences by Google Scholar",
    "naacl": "1th of 20 in computational linguistics conferences by Google Scholar",
    "iclr": "2nd of 20 in Artificial Intelligence conferences by Google Scholar",
    "neurips": "1st of 20 in Artificial Intelligence conferences by Google Scholar",
    "icml": "3rd of 20 in Artificial Intelligence conferences by Google Scholar",
    "aaai": "4th of 20 in Artificial Intelligence conferences by Google Scholar",
    "colm": "New conference, but with many top-tier papers and a very competitive acceptance rate.",
    "tmlr": "New journal, but with many top-tier papers and a very competitive acceptance rate.",
    "lrec": "6th of 20 in computational linguistics conferences by Google Scholar",
    "coling": "5th of 20 in computational linguistics conferences by Google Scholar",
    "cloud": "IEEE Transactions on Cloud Computing, impact factor 5.3",
}

CONFERENCES = set(VENUE_DESCRIPTIONS.keys()) - JOURNALS

_VENUE_SPLIT_RE = re.compile(r'[2\-*^(]')

_CATEGORY_LABELS = {
    "journals":    "Journals",
    "conferences": "Conferences",
    "reviews":     "Reviews",
    "workshops":   "Workshop Articles",
    "drafts":      "ArXiv Articles",
}


class BibCategories(NamedTuple):
    journals: list
    conferences: list
    reviews: list
    workshops: list
    drafts: list


def extract_tags_str(row):
    tags = [RELEVANT_TAGS[col] for col, val in zip(row.index, row)
            if val == 1 and col in RELEVANT_TAGS]
    return "".join(tags)


def remove_pretitle_tags(s):
    return re.sub(r'\s*pretitle\s*=[^}]*},\s*', '', s)


def shorten_booktitle(s):
    return re.sub(r'(\s*booktitle\s*=.*?\d{4})(.*?)(}\s*,\s*\n)', r'\1\3', s, flags=re.DOTALL)


def simplify_venue(name):
    if pd.isna(name) or not name:
        return ""
    s = str(name).lower()
    if "conference on cloud computing" in s and "ieee" in s:
        return "cloud"
    return _VENUE_SPLIT_RE.split(s, maxsplit=1)[0].strip()


def load_citations(citations_path):
    if os.path.exists(citations_path):
        mtime = datetime.fromtimestamp(os.path.getmtime(citations_path))
        if datetime.now() - mtime > timedelta(days=30):
            print(f"Warning: {citations_path} is {(datetime.now()-mtime).days} days old. Consider running update.py.")
        return pd.read_csv(citations_path)
    print(f"Warning: {citations_path} not found. Proceeding with empty citations.")
    return pd.DataFrame(columns=["Title", "Cited by"])


def _build_name2cite(citations, df_names):
    """Map xlsx paper names to citation counts, using fuzzy matching for renamed titles."""
    name2cite = {}
    missing_exact = []
    missing_similar = []
    for _, row in citations[citations["Cited by"].notna()].iterrows():
        title = row["Title"]
        if title in df_names:
            name2cite[title] = row["Cited by"]
        else:
            similar = difflib.get_close_matches(title, df_names, n=1)
            if similar:
                chosen = similar[0]
                name2cite[chosen] = row["Cited by"]
                if normalize_text(title) != normalize_text(chosen):
                    missing_similar.append((title, chosen))
            else:
                missing_exact.append(title)
    if missing_similar:
        print("Cited papers with a similar but non-matching title:")
        for orig, chosen in missing_similar:
            print(f"  orig:   {orig}\n  chosen: {chosen}\n")
    if missing_exact:
        print("Cited papers not found in the xlsx:")
        for t in missing_exact:
            print(f"  {t}")
    return name2cite


def _categorize(venue_simple, is_arxiv, is_review, is_workshop):
    """Return the BibCategories field name for a paper, or None if unknown."""
    if is_arxiv:
        return "drafts"
    if is_review:
        return "reviews"
    if is_workshop:
        return "workshops"
    if venue_simple in JOURNALS:
        return "journals"
    if venue_simple in CONFERENCES:
        return "conferences"
    return None


def _process_entries(parsed, df, name2cite):
    """Build wzmn.bib text and categorise each paper.

    Returns (bib_out, BibCategories, bibs_seen, under_review_count, non_paper_count).
    """
    bib_parts = []
    cats = {field: [] for field in BibCategories._fields}
    bibs_seen = under_review = non_papers = 0

    for dic in parsed:
        matches = df[df["Bib"] == dic["item_name"]]
        if matches.shape[0] > 1:
            print(f"Warning: duplicate xlsx rows for bib key {dic['item_name']!r}, using first")
            matches = matches.iloc[:1]
        if matches.empty:
            continue

        row = matches
        beg = dic["beg"]
        rest = remove_pretitle_tags(shorten_booktitle(dic["rest"]))
        bibs_seen += 1

        if not row["Paper"].item():
            non_papers += 1
            bib_parts.append(beg + rest)
            continue

        row_bib = row["Bib"].item()
        venue_raw = row["Venue"].item() if pd.notna(row["Venue"].item()) else ""
        venue_simple = simplify_venue(venue_raw)
        is_arxiv = "xiv" in venue_raw.lower() or "review" in venue_raw.lower()
        is_workshop = row["Workshop-paper"].item() == 1
        is_review = row["Review, Survey and Position"].item() == 1

        tags = extract_tags_str(row.squeeze())
        cite_count = str(name2cite.get(row["Name"].item(), 0)).replace("*", "").strip()
        rest = "\n    pretitle={" + tags + "}," + rest
        rest = "\n    citations={" + cite_count + "}," + rest

        if not is_arxiv and not is_workshop:
            venue_info = VENUE_DESCRIPTIONS.get(venue_simple, "")
            if venue_info:
                rest = "\n    venueinf={" + venue_info + "}," + rest
            elif venue_simple:
                print(f"Warning: unknown venue {venue_raw!r} (key: {venue_simple!r}) — venueinf omitted")

        category = _categorize(venue_simple, is_arxiv, is_review, is_workshop)
        if category is None:
            print(f"Warning: cannot categorize venue {venue_raw!r} for {row_bib!r}, adding to drafts")
            category = "drafts"
        cats[category].append(row_bib)
        if is_arxiv:
            under_review += 1

        bib_parts.append(beg + rest)

    bib_out = "".join(p + "\n\n" for p in bib_parts)
    bib_out = bib_out.replace(r"{'", r"{\\'")
    return bib_out, BibCategories(**cats), bibs_seen, under_review, non_papers


def _check_coverage(parsed, df, bibs_seen):
    """Warn about xlsx rows whose bib key is absent from orig.bib."""
    bib_keys_in_orig = {d["item_name"] for d in parsed}
    real_bib = df["Bib"].notna() & ~df["Bib"].str.lower().isin(("nan", "none", ""))
    df_with_bib = df[real_bib]
    if bibs_seen != len(df_with_bib):
        print(f"Warning: matched {bibs_seen} bib entries but {len(df_with_bib)} xlsx rows have a Bib key")
        unmatched = df_with_bib[~df_with_bib["Bib"].isin(bib_keys_in_orig)]
        if not unmatched.empty:
            print("  xlsx rows whose Bib key is absent from orig.bib:")
            for _, r in unmatched.iterrows():
                print(f"    {r['Bib']!r:40s}  {str(r['Name'])[:60]}")


def main():
    with open(os.path.join(FILE_DIR, "orig.bib")) as f:
        bib_raw = f.read()

    parsed = parse_bibtex(bib_raw)
    df = read_df()
    name2cite = _build_name2cite(
        load_citations(os.path.join(FILE_DIR, "citations.csv")),
        set(df["Name"].dropna()),
    )

    bib_out, cats, bibs_seen, under_review, non_papers = _process_entries(parsed, df, name2cite)

    enhanced_path = os.path.join(FILE_DIR, "overleaf", "Wzmn.bib")
    with open(enhanced_path, "w") as f:
        f.write(bib_out)

    _check_coverage(parsed, df, bibs_seen)

    print(f"Skipped {under_review} under-review and {non_papers} non-papers")
    print(f"Bib exported to {os.path.abspath(enhanced_path)}")
    print("All venues: " + str([x for x in df["Venue"].apply(simplify_venue).unique()
                                  if x and "xiv" not in x and "review" not in x]))
    for field, keys in cats._asdict().items():
        print(f"% {_CATEGORY_LABELS[field]}:\n\\nocite{{{','.join(keys)}}}")

    return cats


if __name__ == "__main__":
    main()
