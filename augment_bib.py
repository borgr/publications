import re
import pandas as pd
import os

FILE_DIR = os.path.dirname(__file__)


def read_df():
    df = pd.read_excel(
        os.path.join(FILE_DIR, 'Contributions_table.xlsx'))
    df = df.rename(
        columns={'The Science of Deep Learning': 'The Science of\nDeep Learning'})
    x = "Time of publish ID"
    df = df.dropna(subset=[x, 'Name'])
    df = df.sort_values(x)
    df["Name"] = df["Name"].apply(lambda x: x.strip())
    df["Bib"] = df["Bib"].apply(lambda x: str(x).strip() if pd.notna(x) else x)
    return df


def parse_bibtex(bib_string):
    entry_pattern = re.compile(
        r'(@(\w+)\s*\{([^,]+),)(\s*((?:.|\n)*?)(\n\}|}}\n\s*))', re.DOTALL)
    title_pattern = re.compile(r'\btitle\s*=\s*\{(.*?)\},', re.DOTALL)
    results = []
    for beg, entry_type, item_name, rest, entry_content, _ in entry_pattern.findall(bib_string):
        title_match = title_pattern.search(entry_content)
        title = title_match.group(1) if title_match else "Title not found"
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
