#!/usr/bin/python

import sys
import re
from scholarly import scholarly

# from scholarly import ProxyGenerator
# # Set up a ProxyGenerator object to use free proxies
# # This needs to be done only once per session
# pg = ProxyGenerator()
# pg.FreeProxies()
# scholarly.use_proxy(pg)
AUTHOR = "Leshem Choshen"


def main(author=AUTHOR):
    """Ignores the per publication citations, and only updates the total line."""
    total_line, pubs = get_all_citations(author)
    return total_line, pubs
    # print(pubs)

    with open(papers_file) as papers_list, open(ofile, 'w') as ofh:
        for l in papers_list:
            if l.startswith('Total Citations: '):
                l = total_line
            else:
                m = re.search('\\\\gsurl\{([^\}]+)\}', l)

                if m:
                    id = m.group(1)

                    n = pubs[id]
                    l = re.sub('\\\\citations{\d+}', '\\\\citations{'+n+'}', l)

            ofh.write(l)

    return 0


def get_all_citations(author):
    # Retrieve the author's data, fill-in, and print
    # Get an iterator for the author results
    search_query = scholarly.search_author(author)
    # Retrieve the first result from the iterator
    first_author_result = next(search_query)
    scholarly.pprint(first_author_result)

    # Retrieve all the details for the author
    author = scholarly.fill(first_author_result)
#    scholarly.pprint(author)

    pubs = {}

    total = author['citedby']
    h_index = author['hindex']
    i10_index = author['i10index']

    total_line = 'Total Citations: {}, h-index: {}, i10-index: {} (by Google Scholar).'.format(
        total, h_index, i10_index)

    for p in author['publications']:
        # print(p)
        title = p['bib']
        id = p['author_pub_id']
        n = p['num_citations']
#        print(title, "\\\\gsurl{"+id+"}")
        pubs[id] = str(n)

    return total_line, pubs


if __name__ == '__main__':
    main()
