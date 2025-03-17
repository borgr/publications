import difflib
import re
import pandas as pd
import numpy as np
import os
WZMN = True

FILE_DIR = os.path.dirname(__file__)

with open(os.path.join(FILE_DIR, "orig.bib")) as fl:
    bib = fl.read()


def read_df():
    df = pd.read_excel(
        os.path.join(FILE_DIR, 'Contributions_table.xlsx'))
    lines = ['Resources', 'The Science of\nDeep Learning', 'Methods', 'Dataset', 'Training',
             'Evaluation', 'Shared-task\effort', 'Language&Cognition', 'Open',
             'Meta-science', 'Enabling Low Budget Research', 'Efficiency', 'NLP']
    lines = ['NLP', 'Enabling Low Budget Research', 'The Science of\nDeep Learning', 'Methods',
             'Evaluation', 'Open', 'Language&Cognition',  'Resources'
             ]
    df = df.rename(
        columns={'The Science of Deep Learning': 'The Science of\nDeep Learning'})
    x = "Time of publish ID"
    df = df.dropna(subset=[x])
    df = df.sort_values(x)
    df["Name"] = df["Name"].apply(lambda x: x.strip())
    df["Bib"] = df["Bib"].apply(lambda x: str(x).strip() if x else x)
    return df


relevant_tags = {r"inter\eval": "\\UND",
                 "Enabling Low Budget Research": "\\META", "Open": "\\COL", "Language&Cognition": "\\LANG"}


def extract_tags_str(row):
    tags = [relevant_tags[col] for col, tag in zip(
        row.index, row) if tag == 1 and col in relevant_tags]
    tags_str = "".join(tags)
    return tags_str


def entry_to_bibitem(entry_type, item_name, entry_content):
    return f"@{entry_type}{{{item_name},\n{entry_content}\n}}\n"


def remove_pretitle_tags(input_string):
    pattern = r'\s*pretitle\s*=[^}]*},\s*'
    output_string = re.sub(pattern, '', input_string)
    return output_string


def shorten_booktitle(input_string):
    pattern = r'(\s*booktitle\s*=.*?\d\d\d\d)(.*?)(}\s*,\s*\n)'
    # pattern = r'(\s*booktitle\s*=.*?)(.*?)(}\s*,\s*\n)'
    output_string = re.sub(
        pattern, r'\1\3', input_string, flags=re.DOTALL) + "},"
    return output_string


def parse_bibtex(bib_string):
    # Regular expression to match BibTeX entries
    entry_pattern = re.compile(
        r'(@(\w+)\s*\{([^,]+),)(\s*((?:.|\n)*?)\n\})', re.DOTALL)
    # re.compile(
    #    r'@(\w+)\s*\{([^,]+),\s*((?:.|\n)*?)\n\}', re.DOTALL)

    # Regular expression to match the title field
    title_pattern = re.compile(r'\btitle\s*=\s*\{(.*?)\},', re.DOTALL)

    results = []

    # Find all entries in the BibTeX string
    for beg, entry_type, item_name, rest, entry_content in entry_pattern.findall(bib_string):
        # Search for the title within the entry content
        title_match = title_pattern.search(entry_content)
        title = title_match.group(1) if title_match else "Title not found"

        # Remove any newlines and extra spaces from the title
        title = ' '.join(title.split())

        results.append({
            "item_name": item_name.strip(),
            "title": title,
            "type": entry_type,
            "content": entry_content,
            "beg": beg,
            "rest": rest,
        })

    return results


parsed = parse_bibtex(bib)
with open("tmp.csv", "w") as fl:
    for dic in parsed:
        fl.write(f'{dic["item_name"]},{dic["title"]}\n')
df = read_df()


def simplify_venue(name):
    return name.split("2")[0].split("-")[0].split("*")[0].split("^")[0].split("(")[0].strip().lower()


def simplify_text_for_comparison(txt):
    return re.sub('[\W_ ]+', '', txt.lower().strip())


venue2descripton = {"JML": "impact factor 4.014",
                    "tacl": """11th out of 145 journals in the "computer science (artificial intelligence)" category, with an impact factor of 10.9""",
                    "nature": "The top venue in the world under many metrics, with an impact factor of 50.5",
                    "nature machine intelligence": "Journal with an 18.8 impact factor, higher than the top journals and conferences in Main Machine Learning venues.",
                    "conll": "11th of 20 in computational linguistics conferences by Google Scholar",
                    "acl": "1th of 20 in computational linguistics conferences by Google Scholar",
                    "emnlp": "1th of 20 in computational linguistics conferences by Google Scholar",
                    "naacl": "1th of 20 in computational linguistics conferences by Google Scholar",
                    "iclr": "2nd of 20 in Artificial Intelligence conferences by Google Scholar",
                    "neurips": "1st of 20 in Artificial Intelligence conferences by Google Scholar",
                    "icml": "3rd of 20 in Artificial Intelligence conferences by Google Scholar",
                    "aaai": "4th of 20 in Artificial Intelligence conferences by Google Scholar",
                    "colm": "",
                    "lrec": "6th of 20 in computational linguistics conferences by Google Scholar",
                    "coling": "5th of 20 in computational linguistics conferences by Google Scholar",
                    }
journals = {"JML", "tacl", "nature", "nature machine intelligence"}
conferences = set(set(venue2descripton.keys()) - journals)
venue2descripton = {simplify_venue(
    key): val for key, val in venue2descripton.items()}


print("All venues:" + str([x for x in df["Venue"].apply(simplify_venue).unique()
      if "xiv" not in x.lower() and "review" not in x.lower()]))

# manually copy pasted from Google Scholar, export ignores the citations...
citations_path = os.path.join(FILE_DIR, "citations.csv")
citations = pd.read_csv(citations_path)
name2cite = {}
for i, row in citations[citations["Cited by"].notna()].iterrows():
    title = row["Title"]
    if title in df["Name"].unique():
        name2cite[title] = row["Cited by"]
    else:
        similar = difflib.get_close_matches(title, df["Name"].unique(), n=1)
        if similar:
            similar = similar[0]
            name2cite[similar] = row["Cited by"]
            if simplify_text_for_comparison(title) != simplify_text_for_comparison(similar):
                print(
                    f"Cited paper wasn't found in the manual list orig,chosen:\n{title}\n {similar}")
        else:
            print(
                f"Cited paper wasn't found in the manual list\n{title}\n and no similar title was found either")

bibs_seen = 0
under_review = 0
non_papers = 0
bib = ""
journal_bibs = []
conference_bibs = []
review_bibs = []
workshop_bibs = []
draft_bibs = []
for dic in parsed:
    row = df[df["Bib"] == dic["item_name"]]
    beg = dic["beg"]
    rest = remove_pretitle_tags(dic["rest"])
    if row.empty:  # my papers only
        continue
    if WZMN:
        rest = shorten_booktitle(dic["rest"])
    bibs_seen += 1
    venue = row["Venue"].item().lower()

    if not row["Paper"].item():
        non_papers += 1
    elif not row.empty:
        row_bib = row["Bib"].item()
        tags = extract_tags_str(row.squeeze())
        rest = "\n    pretitle={"+tags+"}," + rest
        rest = "\n    citations={" + \
            str(name2cite.get(row["Name"].item(), 0)).replace(
                "*", "").strip()+"}," + rest
        if "xiv" in venue or "review" in venue:
            under_review += 1
        elif row["Workshop-paper"].item() != 1:
            rest = "\n    venueinf={" + \
                venue2descripton[simplify_venue(
                    row["Venue"].item())]+"}," + rest

        # Where to cite
        if "xiv" in venue or "review" in venue:
            draft_bibs.append(row_bib)
        elif row["Review"].item() == 1:
            review_bibs.append(row_bib)
        elif row["Workshop-paper"].item() == 1:
            workshop_bibs.append(row_bib)
        else:
            if simplify_venue(venue) in journals:
                journal_bibs.append(row_bib)
            elif simplify_venue(venue) in conferences:
                conference_bibs.append(row_bib)
            else:
                raise f"Unknown venue {venue}"
    else:
        if "eshem" in rest:
            print("skipped item for unclear reason", dic["item_name"])
            raise
    bib += beg+rest+"\n\n"

bib = bib.replace(r"{'", r"{\'")
enhanced_path = os.path.join(FILE_DIR, "huji.bib")
with open(enhanced_path, "w") as fl:
    fl.write(bib)
if bibs_seen != len(df):
    print(
        f"Warning, seen {bibs_seen} bibs in bibs, but the manually annotated table contains {len(df)}")
print(
    f"skipped {under_review} papers under review and {non_papers} non papers (workshops etc.)")
# bib = ""
# for dic in parsed:
#     row = df[df["Bib"] == dic["item_name"]]
#     content = dic["content"]
#     if not row.empty:
#         tags = extract_tags_str(row.squeeze())
#         content = "    pretitle={"+tags+"},\n" + content
#     else:
#         if "eshem" in content and "Xiv" not in content:
#             print(dic["item_name"])
#     bib += entry_to_bibitem(dic["type"], dic["item_name"], content)
# enhanced_path = os.path.join(FILE_DIR, "enhanced.bib")
# with open(enhanced_path, "w") as fl:
#     fl.write(bib)
print(f"bib exported to {os.path.abspath(enhanced_path)}")
print("Journals:\n\\nocite{"+",".join(journal_bibs) + "}")
print("Conferences:\n\\nocite{"+",".join(conference_bibs) + "}")
print("Reviews:\n\\nocite{"+",".join(review_bibs) + "}")
print("Workshop Articles:\n\\nocite{"+",".join(workshop_bibs) + "}")
print("ArXiv Articles:\n\\nocite{"+",".join(draft_bibs) + "}")
