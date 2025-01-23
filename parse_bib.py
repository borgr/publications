import re
import pandas as pd
import numpy as np
import os

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
    pattern = r'\s*pretitle=[^}]*},\s*'
    output_string = re.sub(pattern, '', input_string)
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


bib = ""
for dic in parsed:
    row = df[df["Bib"] == dic["item_name"]]
    beg = dic["beg"]
    rest = remove_pretitle_tags(dic["rest"])
    if not row.empty:
        tags = extract_tags_str(row.squeeze())
        rest = "\n    pretitle={"+tags+"}," + rest
    else:
        if "eshem" in rest and "Xiv" not in rest:
            print(dic["item_name"])
    bib += beg+rest+"\n\n"
bib = bib.replace(r"{'", r"{\'")
enhanced_path = os.path.join(FILE_DIR, "enhanced.bib")
with open(enhanced_path, "w") as fl:
    fl.write(bib)


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
